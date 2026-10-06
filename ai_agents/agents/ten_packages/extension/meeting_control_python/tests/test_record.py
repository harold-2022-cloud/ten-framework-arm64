#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The meeting record: topics, what was said in them, and where in the file.

Every time in the record is a position in the uploaded file. A topic knows
where it starts; an utterance knows where it starts inside its topic. The
two add up to the place a person can seek to in audio.ogg.
"""

import os
import time

import pytest

from meeting_control_python.record import MeetingRecord


def said(start, end, speaker, text, embedding=None):
    return {
        "start_s": start,
        "end_s": end,
        "speaker": speaker,
        "text": text,
        "embedding": embedding,
    }


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


def test_a_line_is_placed_at_its_topic_start_plus_its_own_offset():
    record = MeetingRecord("m1")
    record.add_topic("t02", 300.0, 600.0, [said(12.5, 15.0, 0, "好。")])

    assert record.lines_for("t02") == "[05:12] 說話人1：好。"


def test_with_a_recording_start_the_wall_clock_comes_first(taipei):
    # 2026-09-29 14:30:00 in Taipei
    record = MeetingRecord("m1", recorded_at=1790663400.0)
    record.add_topic("t02", 300.0, 600.0, [said(12.5, 15.0, 1, "下週出版本。")])

    assert record.lines_for("t02") == "[14:35 / 05:12] 說話人2：下週出版本。"


def test_turns_come_out_in_meeting_time_with_their_topic_and_voice():
    record = MeetingRecord("m1")
    record.add_topic(
        "t02", 300.0, 600.0, [said(10.0, 12.0, 1, "乙", [0.0, 1.0])]
    )
    record.add_topic("t01", 0.0, 300.0, [said(5.0, 9.0, 0, "甲", [1.0, 0.0])])

    turns = record.turns()

    assert [(t.topic, t.local, t.start_s, t.end_s) for t in turns] == [
        (0, 0, 5.0, 9.0),
        (1, 1, 310.0, 312.0),
    ]
    assert turns[1].embedding == [0.0, 1.0]


def test_relabelling_writes_meeting_wide_numbers_back():
    record = MeetingRecord("m1")
    record.add_topic(
        "t01", 0.0, 300.0, [said(5.0, 9.0, 0, "甲"), said(9, 10, 1, "乙")]
    )
    record.add_topic("t02", 300.0, 600.0, [said(10.0, 12.0, 0, "乙")])

    record.relabel([0, 1, 1])

    assert record.lines_for("t02") == "[05:10] 說話人2：乙"
    assert record.speaker_count == 2


def test_a_failed_topic_keeps_its_place_and_says_why():
    record = MeetingRecord("m1")
    record.mark_failed("t03", 600.0, 900.0, "diarization ran out of memory")

    assert record.lines_for("t03") == (
        "（10:00–15:00 這一段未能處理：diarization ran out of memory）"
    )


def test_the_json_record_has_no_embeddings_and_topics_in_order():
    record = MeetingRecord("m1", title="週會", duration_s=900.0)
    record.add_topic("t02", 300.0, 600.0, [said(1.0, 2.0, 0, "乙", [0.6, 0.8])])
    record.add_topic("t01", 0.0, 300.0, [said(1.0, 2.0, 0, "甲", [0.6, 0.8])])
    record.add_summary("t01", "開場。")

    out = record.to_json(summary="整場結論。", actions=[], actions_error=None)

    assert [t["id"] for t in out["topics"]] == ["t01", "t02"]
    assert out["topics"][0]["summary"] == "開場。"
    assert "embedding" not in out["topics"][0]["utterances"][0]
    assert out["summary"] == "整場結論。"
    assert out["title"] == "週會"
    assert out["speaker_count"] == 1
