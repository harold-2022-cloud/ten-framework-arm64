#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""What the uploader keeps on disk, and what it lets in.

One folder per meeting under meetings_dir: audio.ogg, record.json and
minutes.txt are the archive; work/state.json is how far processing got.
The uploader writes only the first state, received; the controller writes
the rest, and the uploader reads them back for GET /meeting/{id}.
"""

import json
import os
import time

import numpy as np
import pytest
import soundfile as sf

from meeting_uploader import store


@pytest.fixture
def taipei():
    old = os.environ.get("TZ")
    # POSIX form, UTC+8: needs no tzdata, which the dev image lacks.
    os.environ["TZ"] = "CST-8"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


def tone(seconds=1.0, rate=16000, channels=1):
    t = np.arange(int(seconds * rate)) / rate
    x = 0.3 * np.sin(2 * np.pi * 440 * t)
    return np.stack([x] * channels, axis=1) if channels > 1 else x


def write(path, rate=16000, channels=1, fmt="OGG", subtype="OPUS"):
    sf.write(
        str(path),
        tone(rate=rate, channels=channels),
        rate,
        format=fmt,
        subtype=subtype,
    )
    return str(path)


def put_state(folder, **fields):
    work = folder / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "state.json").write_text(json.dumps(fields), encoding="utf-8")


def test_an_id_is_made_from_the_arrival_time(taipei):
    # 2026-09-29 14:30:00 in Taipei
    assert store.new_id(1790663400.0) == "20260929-143000"


@pytest.mark.parametrize("good", ["m1", "20260929-143000", "week_42"])
def test_a_plain_id_names_a_folder(good):
    assert store.valid_id(good)


@pytest.mark.parametrize("bad", ["", ".", "..", "../x", "a/b", "a b", "x" * 65])
def test_an_id_that_could_leave_meetings_dir_is_refused(bad):
    assert not store.valid_id(bad)


def test_ogg_opus_at_16k_mono_is_let_in(tmp_path):
    assert store.check_audio(write(tmp_path / "a.ogg")) is None


def test_a_wav_is_turned_away_by_name(tmp_path):
    reason = store.check_audio(
        write(tmp_path / "a.wav", fmt="WAV", subtype="PCM_16")
    )

    assert "WAV" in reason


def test_the_wrong_rate_is_turned_away_with_the_rate(tmp_path):
    reason = store.check_audio(write(tmp_path / "a.ogg", rate=48000))

    assert "48000" in reason


def test_stereo_is_turned_away(tmp_path):
    reason = store.check_audio(write(tmp_path / "a.ogg", channels=2))

    assert "2 channels" in reason


def test_bytes_that_are_not_audio_are_turned_away(tmp_path):
    path = tmp_path / "a.ogg"
    path.write_bytes(b"not audio at all")

    assert "not a readable audio file" in store.check_audio(str(path))


def test_landing_moves_the_upload_in_and_clears_the_last_run(tmp_path):
    folder = tmp_path / "m1"
    put_state(folder, state="archived")
    (folder / "work" / "audio.pcm").write_bytes(b"\x00" * 10)
    (folder / "record.json").write_text("{}", encoding="utf-8")
    (folder / "minutes.txt").write_text("old", encoding="utf-8")
    upload = tmp_path / "incoming.ogg"
    upload.write_bytes(b"new audio")

    ogg_path, work_dir = store.land(str(upload), str(folder))

    assert ogg_path == str(folder / "audio.ogg")
    assert (folder / "audio.ogg").read_bytes() == b"new audio"
    assert not upload.exists()
    assert not (folder / "record.json").exists()
    assert not (folder / "minutes.txt").exists()
    assert work_dir == str(folder / "work")
    assert os.listdir(work_dir) == []


def test_a_meeting_in_progress_reports_its_progress(tmp_path):
    put_state(
        tmp_path / "m1",
        state="transcribing",
        topics_done=3,
        topics_total=11,
        error=None,
    )

    assert store.status(str(tmp_path / "m1"), "m1") == {
        "meeting_id": "m1",
        "state": "transcribing",
        "progress": {"topics_done": 3, "topics_total": 11},
        "record": None,
        "error": None,
    }


def test_a_finished_meeting_carries_its_record(tmp_path):
    folder = tmp_path / "m1"
    put_state(folder, state="archived", topics_done=2, topics_total=2)
    (folder / "record.json").write_text(
        json.dumps({"summary": "下週出版本。"}), encoding="utf-8"
    )

    assert store.status(str(folder), "m1")["record"] == {
        "summary": "下週出版本。"
    }


def test_a_meeting_never_uploaded_has_no_status(tmp_path):
    assert store.status(str(tmp_path / "nobody"), "nobody") is None


def test_meetings_are_listed_newest_first(tmp_path):
    put_state(tmp_path / "a", state="archived", title="週會", received_at=100)
    put_state(tmp_path / "b", state="transcribing", received_at=200)
    (tmp_path / "stray-file").write_text("x", encoding="utf-8")

    assert store.list_meetings(str(tmp_path)) == [
        {
            "meeting_id": "b",
            "state": "transcribing",
            "title": None,
            "received_at": 200,
        },
        {
            "meeting_id": "a",
            "state": "archived",
            "title": "週會",
            "received_at": 100,
        },
    ]


def test_a_meeting_cut_off_by_a_restart_is_marked_failed(tmp_path):
    put_state(tmp_path / "cut", state="transcribing", topics_done=3)
    put_state(tmp_path / "done", state="archived")

    stopped = store.fail_interrupted(str(tmp_path))

    assert stopped == ["cut"]
    cut = store.read_state(str(tmp_path / "cut"))
    assert cut["state"] == "failed"
    assert "upload it again" in cut["error"]
    assert cut["topics_done"] == 3  # where it stopped is kept
    assert store.read_state(str(tmp_path / "done"))["state"] == "archived"


def test_a_landed_meeting_is_written_fresh_with_what_the_upload_said(
    tmp_path,
):
    folder = tmp_path / "m1"
    (folder / "work").mkdir(parents=True)
    details = {
        "title": "週會",
        "speakers": 6,
        "recorded_at": None,
        "script": None,
    }

    store.write_queued(
        str(folder), "m1", details, received_at=100.0, duration_s=61.5
    )

    assert store.read_upload(str(folder)) == {**details, "duration_s": 61.5}
    assert store.read_state(str(folder)) == {
        "meeting_id": "m1",
        "state": "queued",
        "title": "週會",
        "received_at": 100.0,
        "topics_done": 0,
        "topics_total": 0,
        "error": None,
        "updated_at": store.read_state(str(folder))["updated_at"],
    }


def test_a_meeting_waiting_in_the_queue_is_not_marked_interrupted(tmp_path):
    # Its upload is whole on disk: the next worker takes it in turn.
    waiting = tmp_path / "waiting"
    going = tmp_path / "going"
    for folder, state in ((waiting, "queued"), (going, "transcribing")):
        (folder / "work").mkdir(parents=True)
        store.write_state(str(folder), state=state)

    stopped = store.fail_interrupted(str(tmp_path))

    assert stopped == ["going"]
    assert store.read_state(str(waiting))["state"] == "queued"
