#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""work/state.json: the one place GET /meeting/{id} reads progress from."""

import json

from meeting_control_python.state import write_state


def read(work_dir):
    return json.loads((work_dir / "state.json").read_text(encoding="utf-8"))


def test_the_state_lands_where_the_uploader_reads_it(tmp_path):
    write_state(str(tmp_path), "decoding")

    assert read(tmp_path)["state"] == "decoding"


def test_a_later_write_keeps_what_earlier_ones_said(tmp_path):
    write_state(str(tmp_path), "transcribing", topics_total=5, topics_done=0)
    write_state(str(tmp_path), "transcribing", topics_done=2)

    state = read(tmp_path)
    assert (state["topics_done"], state["topics_total"]) == (2, 5)


def test_no_temporary_file_is_left_behind(tmp_path):
    write_state(str(tmp_path), "decoding")
    write_state(str(tmp_path), "archived")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
