#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""disk.py: a missing, empty or broken file is "not yet", never an error;
only ids the uploader would take are read."""

import json
import os

from meeting_screen import disk


def put(root, meeting_id, state=None, upload=None, record=None, raw=None):
    folder = os.path.join(root, meeting_id)
    os.makedirs(os.path.join(folder, "work"), exist_ok=True)
    for name, value in (
        ("work/state.json", state),
        ("upload.json", upload),
        ("record.json", record),
    ):
        if value is not None:
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                json.dump(value, f)
    for name, text in (raw or {}).items():
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(text)
    return folder


def test_no_folder_is_no_meetings(tmp_path):
    assert disk.list_meetings(str(tmp_path / "nowhere")) == []


def test_a_folder_without_a_state_is_not_a_meeting_yet(tmp_path):
    put(str(tmp_path), "m1")
    assert disk.list_meetings(str(tmp_path)) == []


def test_an_empty_or_broken_or_odd_state_is_not_yet(tmp_path):
    put(str(tmp_path), "m1", raw={"work/state.json": ""})
    put(str(tmp_path), "m2", raw={"work/state.json": "{not json"})
    put(str(tmp_path), "m3", raw={"work/state.json": "[1, 2]"})
    assert disk.list_meetings(str(tmp_path)) == []


def test_no_upload_is_an_empty_one(tmp_path):
    put(str(tmp_path), "m1", state={"state": "queued", "received_at": 5})
    (m,) = disk.list_meetings(str(tmp_path))
    assert (m.meeting_id, m.upload, m.received_at) == ("m1", {}, 5.0)


def test_a_missing_or_broken_record_is_none(tmp_path):
    put(str(tmp_path), "m1", state={"state": "transcribing"})
    put(
        str(tmp_path),
        "m2",
        state={"state": "transcribing"},
        raw={"record.json": "{"},
    )
    assert disk.read_record(str(tmp_path), "m1") is None
    assert disk.read_record(str(tmp_path), "m2") is None


def test_a_partial_record_is_read_as_it_is(tmp_path):
    put(
        str(tmp_path),
        "m1",
        state={"state": "transcribing"},
        record={"topics": []},
    )
    assert disk.read_record(str(tmp_path), "m1") == {"topics": []}


def test_only_ids_the_uploader_takes(tmp_path):
    put(str(tmp_path), ".hidden", state={"state": "queued"})
    put(str(tmp_path), "ok-1", state={"state": "queued"})
    names = [m.meeting_id for m in disk.list_meetings(str(tmp_path))]
    assert names == ["ok-1"]
    for bad in ("..", ".", "a/b", "../etc", "", "x" * 65, None, 5):
        assert not disk.valid_id(bad)
        assert disk.read_meeting(str(tmp_path), bad) is None
        assert disk.read_record(str(tmp_path), bad) is None


def test_newest_received_first(tmp_path):
    put(str(tmp_path), "a", state={"state": "archived", "received_at": 10})
    put(str(tmp_path), "b", state={"state": "archived", "received_at": 30})
    put(str(tmp_path), "c", state={"state": "archived"})
    assert [m.meeting_id for m in disk.list_meetings(str(tmp_path))] == [
        "b",
        "a",
        "c",
    ]
