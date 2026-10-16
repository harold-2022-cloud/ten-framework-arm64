#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The screen reads what the board's own writers write: the uploader's store
and the controller's state and record (meeting_uploader/store.py,
meeting_control_python/state.py, record.py). Should either change its
format, these fail."""

import asyncio
import json
import os
import time

from conftest import writer
from meeting_screen import disk, view

store = writer("meeting_uploader", "store")
cstate = writer("meeting_control_python", "state")
crecord = writer("meeting_control_python", "record")
cflow = writer("meeting_control_python", "flow")
cconfig = writer("meeting_control_python", "config")


class Quiet:
    """The extensions around the controller, doing nothing."""

    async def segment_audio(self, *_):
        pass

    async def transcribe(self, *_):
        pass

    async def ask_llm(self, _):
        return ""

    def log(self, _):
        pass


UPLOAD = {
    "title": "週會",
    "speakers": 3,
    "recorded_at": None,
    "script": "traditional",
}


def landed(tmp_path, received_at):
    folder = tmp_path / "m1"
    (folder / "work").mkdir(parents=True)
    store.write_queued(
        str(folder),
        "m1",
        dict(UPLOAD),
        received_at=received_at,
        duration_s=120.0,
    )
    return str(folder)


def shown(tmp_path, now):
    meetings = disk.list_meetings(str(tmp_path))
    return view.build(
        meetings, lambda i: disk.read_record(str(tmp_path), i), now
    )


def persist(folder, rec):
    with open(os.path.join(folder, "record.json"), "w", encoding="utf-8") as f:
        json.dump(
            rec.to_json(summary="", actions=[], actions_error=None),
            f,
            ensure_ascii=False,
        )


def test_a_queued_upload(tmp_path):
    landed(tmp_path, 900.0)
    v = shown(tmp_path, 1000.0)
    meeting = v["meeting"]
    assert v["scene"] == "queued"
    assert (
        meeting["title"],
        meeting["duration_s"],
        meeting["received_at"],
        meeting["stage"],
    ) == ("週會", 120.0, 900.0, 0)


def test_handed_on_to_the_controller(tmp_path):
    folder = landed(tmp_path, 900.0)
    store.write_state(folder, state="received", started_at=950.0)
    meeting = shown(tmp_path, 1000.0)["meeting"]
    assert (meeting["stage"], meeting["started_at"], meeting["elapsed_s"]) == (
        1,
        950.0,
        50.0,
    )


def test_transcribing_with_a_partial_record(tmp_path):
    folder = landed(tmp_path, 900.0)
    store.write_state(folder, state="received", started_at=950.0)
    rec = crecord.MeetingRecord("m1", title="週會", duration_s=120.0)
    rec.add_topic(
        "t01",
        0.0,
        60.0,
        [
            {
                "speaker": 0,
                "start_s": 1.0,
                "end_s": 8.0,
                "text": "我們先定時間。",
            }
        ],
    )
    persist(folder, rec)
    cstate.write_state(
        os.path.join(folder, "work"),
        "transcribing",
        topics_done=1,
        topics_total=2,
    )
    meeting = shown(tmp_path, 1000.0)["meeting"]
    assert (meeting["stage"], meeting["speakers_final"]) == (2, False)
    assert meeting["progress"] == {"topics_done": 1, "topics_total": 2}
    u = meeting["record"]["topics"][0]["utterances"][0]
    assert (u["speaker"], u["start_s"], u["end_s"], u["text"]) == (
        0,
        1.0,
        8.0,
        "我們先定時間。",
    )
    assert meeting["record"]["topics"][0]["start_s"] == 0.0


def test_summarising_makes_the_speakers_final(tmp_path):
    folder = landed(tmp_path, 900.0)
    cstate.write_state(
        os.path.join(folder, "work"),
        "summarising",
        topics_done=0,
        topics_total=2,
    )
    meeting = shown(tmp_path, 1000.0)["meeting"]
    assert (meeting["stage"], meeting["speakers_final"]) == (5, True)


def test_archived_is_the_result_with_its_processing_time(tmp_path):
    now = time.time()
    folder = landed(tmp_path, now - 60)
    store.write_state(folder, state="received", started_at=now - 50)
    ended = cstate.write_state(os.path.join(folder, "work"), "archived")
    v = shown(tmp_path, now + 1)
    meeting = v["meeting"]
    assert v["scene"] == "result"
    assert meeting["finished_at"] == ended["updated_at"]
    assert meeting["elapsed_s"] == ended["updated_at"] - (now - 50)


def test_failed_with_its_reason(tmp_path):
    folder = landed(tmp_path, 900.0)
    cstate.write_state(
        os.path.join(folder, "work"), "failed", error="ASR failed"
    )
    v = shown(tmp_path, 1000.0)
    assert (v["scene"], v["meeting"]["error"]) == ("failed", "ASR failed")


def test_the_controllers_steps_light_their_stages(tmp_path):
    folder = landed(tmp_path, 900.0)
    flow = cflow.MeetingFlow(cconfig.MeetingControlConfig(), Quiet())

    async def go():
        await flow.start(
            {
                "meeting_id": "m1",
                "ogg_path": os.path.join(folder, "audio.ogg"),
                "work_dir": os.path.join(folder, "work"),
                "title": "週會",
                "speakers": 2,
                "script": "traditional",
            }
        )
        await flow.on_segments_ready(
            {
                "pcm_path": os.path.join(folder, "work", "audio.pcm"),
                "duration_s": 120.0,
                "segments": [{"id": "t01", "start_s": 0.0, "end_s": 120.0}],
                "error": None,
            }
        )
        diarizing = shown(tmp_path, time.time())["meeting"]
        await flow.on_progress(
            {
                "segment_id": "t01",
                "step": "recognising",
                "turns_done": 1,
                "turns_total": 4,
                "utterance": {
                    "start_s": 1.0,
                    "end_s": 3.0,
                    "speaker": 0,
                    "text": "我們先定時間。",
                },
            }
        )
        return diarizing, shown(tmp_path, time.time())["meeting"]

    diarizing, recognising = asyncio.run(go())
    assert (diarizing["stage"], diarizing["topic_s"]) == (2, 120.0)
    assert (recognising["stage"], recognising["turns"]) == (
        3,
        {"done": 1, "total": 4},
    )
    words = recognising["record"]["topics"][0]["utterances"]
    assert words[0]["text"] == "我們先定時間。"
    assert recognising["speakers_final"] is False
