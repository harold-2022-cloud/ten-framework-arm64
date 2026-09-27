#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The record is the product. Everything else exists to fill it in."""

import pytest

from main_python.record import MeetingRecord

STARTED_AT = 1774598700.0  # 2026-03-27 14:05:00 local


def test_the_record_carries_both_clocks():
    """Absolute time is for a person reading it; the offset is for finding the
    moment in the recording."""
    record = MeetingRecord()
    record.add_segment(
        "seg-1",
        started_at=STARTED_AT + 30,
        utterances=[
            {
                "start_s": 2.0,
                "end_s": 6.0,
                "speaker": 1,
                "text": "下週要出版本。",
            },
        ],
    )
    lines = record.as_prompt_lines(started_at=STARTED_AT)
    assert "說話人1: 下週要出版本。" in lines
    assert "/ 00:32]" in lines, "the offset from the meeting's own start"


def test_a_failed_segment_does_not_take_the_others_with_it():
    """The reason for cutting a meeting into topics at all."""
    record = MeetingRecord()
    record.add_segment(
        "seg-1",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "一。"}
        ],
    )
    record.mark_failed("seg-2", "diarization ran out of memory")
    record.add_segment(
        "seg-3",
        started_at=STARTED_AT + 600,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 1, "text": "三。"}
        ],
    )

    lines = record.as_prompt_lines(started_at=STARTED_AT)
    assert "一。" in lines and "三。" in lines
    assert "seg-2 段未能處理" in lines
    assert not record.is_empty


def test_segments_are_ordered_by_when_they_happened_not_when_they_finished():
    """A short segment can finish transcribing after a long earlier one."""
    record = MeetingRecord()
    record.add_segment(
        "late-but-earlier",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 0.0, "end_s": 1.0, "speaker": 0, "text": "先。"}
        ],
    )
    record.add_segment(
        "early-but-later",
        started_at=STARTED_AT + 300,
        utterances=[
            {"start_s": 0.0, "end_s": 1.0, "speaker": 0, "text": "後。"}
        ],
    )
    assert [s.segment_id for s in record.ordered()] == [
        "late-but-earlier",
        "early-but-later",
    ]


def test_a_record_with_nothing_in_it_says_so():
    record = MeetingRecord()
    record.mark_failed("seg-1", "everything went wrong")
    assert record.is_empty, "a record of failures is not a meeting record"


import asyncio
from unittest.mock import AsyncMock, MagicMock

from main_python.extension import MeetingControlExtension


def make_extension(**overrides):
    ext = MeetingControlExtension("main_control")
    ext.ten_env = MagicMock()
    ext.ten_env.log_info = MagicMock()
    ext.ten_env.log_error = MagicMock()
    ext._close_segment = AsyncMock()
    ext._assemble = AsyncMock()
    from main_python.config import MeetingControlConfig

    ext.config = MeetingControlConfig(**overrides)
    ext._tick_s = 0.02  # the test's clock, not a meeting's
    ext._ticker = asyncio.create_task(ext._tick())
    return ext


async def stop(ext):
    ext._stopped = True
    ext._ticker.cancel()


@pytest.mark.asyncio
async def test_a_pause_ends_a_topic_and_not_the_meeting():
    """The mistake an earlier design would have made: a meeting has silences
    in it, and treating the first one as the end throws the rest away."""
    ext = make_extension(segment_silence_s=0.05, meeting_silence_s=10.0)
    ext.speech_started()
    ext.speech_stopped()
    await asyncio.sleep(0.15)
    await stop(ext)

    assert ext._close_segment.await_count == 1, "the topic did not close"
    assert ext._assemble.await_count == 0, "the meeting was declared over"


@pytest.mark.asyncio
async def test_a_long_enough_silence_ends_the_meeting():
    ext = make_extension(segment_silence_s=0.05, meeting_silence_s=0.15)
    ext.speech_started()
    ext.speech_stopped()
    await asyncio.sleep(0.30)
    await stop(ext)

    assert ext._assemble.await_count == 1


@pytest.mark.asyncio
async def test_speaking_again_puts_the_topic_off():
    ext = make_extension(segment_silence_s=0.10, meeting_silence_s=1.0)
    ext.speech_started()
    ext.speech_stopped()
    await asyncio.sleep(0.04)
    ext.speech_started()  # somebody carried on
    await asyncio.sleep(0.06)
    await stop(ext)

    assert ext._close_segment.await_count == 0
    assert ext._assemble.await_count == 0


@pytest.mark.asyncio
async def test_the_upload_dying_mid_sentence_still_produces_a_record():
    """A network drop sends no end_of_sentence. Timers armed on that event
    would never be armed, and the meeting would hang there for ever."""
    ext = make_extension(segment_silence_s=0.05, meeting_silence_s=0.15)
    ext.speech_started()  # and then the socket dies: no speech_stopped
    await asyncio.sleep(0.30)
    await stop(ext)

    assert ext._close_segment.await_count == 1
    assert ext._assemble.await_count == 1, "a disconnect left no record"
