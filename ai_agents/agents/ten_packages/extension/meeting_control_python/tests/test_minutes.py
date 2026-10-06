#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""minutes.txt: the record a person forwards to someone who was not there."""

from meeting_control_python.minutes import render_minutes
from meeting_control_python.record import MeetingRecord


def meeting():
    record = MeetingRecord("m1", title="週會", duration_s=900.0)
    record.add_topic(
        "t01",
        0.0,
        300.0,
        [
            {"start_s": 5.0, "end_s": 9.0, "speaker": 0, "text": "開始吧。"},
            {"start_s": 20.0, "end_s": 25.0, "speaker": 1, "text": "好。"},
        ],
    )
    record.add_summary("t01", "確認議程。")
    record.mark_failed("t02", 300.0, 900.0, "timeout")
    return record


def test_the_minutes_open_with_title_length_and_people():
    text = render_minutes(meeting(), summary="下週出版本。", actions=[])

    head = text.splitlines()[:3]
    assert head[0] == "週會"
    assert "15 分鐘" in head[1]
    assert "與會 2 人" in head[1]


def test_the_conclusion_comes_before_the_topics():
    text = render_minutes(meeting(), summary="下週出版本。", actions=[])

    assert text.index("下週出版本。") < text.index("確認議程。")


def test_each_topic_shows_its_range_summary_and_lines():
    text = render_minutes(meeting(), summary="", actions=[])

    assert "00:00–05:00" in text
    assert "[00:05] 說話人1：開始吧。" in text
    assert "05:00–15:00 這一段未能處理：timeout" in text


def test_action_items_get_a_section_only_when_there_are_some():
    without = render_minutes(meeting(), summary="", actions=[])
    with_one = render_minutes(
        meeting(),
        summary="",
        actions=[
            {
                "what": "寫說明",
                "owner": "說話人2",
                "due_raw": "週五",
                "raised_at": "12:30",
            }
        ],
    )

    assert "待辦" not in without
    assert "寫說明" in with_one and "說話人2" in with_one
