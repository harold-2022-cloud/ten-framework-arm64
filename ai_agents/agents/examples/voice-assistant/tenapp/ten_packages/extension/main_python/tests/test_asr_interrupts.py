#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""_on_asr_result has to consult the gate, not re-derive the rule itself."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from main_python.extension import MainControlExtension


def make_extension():
    ext = MainControlExtension("main_control")
    ext.ten_env = MagicMock()
    ext.ten_env.log_info = MagicMock()
    ext.agent = AsyncMock()
    ext._interrupt = AsyncMock()
    ext._send_transcript = AsyncMock()
    return ext


class _Result:
    def __init__(self, text, final):
        self.text = text
        self.final = final
        self.metadata = {"session_id": "123"}


@pytest.mark.asyncio
async def test_a_repeated_partial_interrupts_once():
    ext = make_extension()
    for _ in range(3):
        await ext._on_asr_result(_Result("Who are you", final=False))
    assert ext._interrupt.await_count == 1


class _Speaking:
    def __init__(self, speaking):
        self.speaking = speaking


@pytest.mark.asyncio
async def test_the_speaking_event_reaches_the_gate():
    """The plumbing, not the rule: agent.py used to discard this event."""
    ext = make_extension()
    await ext._on_tts_speaking(_Speaking(True))
    for text in (" 如果", " 如果要", " 如果用于"):
        await ext._on_asr_result(_Result(text, final=False))

    assert ext._interrupt.await_count == 0

    await ext._on_tts_speaking(_Speaking(False))
    await ext._on_asr_result(_Result(" 如果用于", final=False))
    assert ext._interrupt.await_count == 1


@pytest.mark.asyncio
async def test_a_final_still_interrupts_while_speaking():
    ext = make_extension()
    await ext._on_tts_speaking(_Speaking(True))
    await ext._on_asr_result(_Result("停一下", final=True))
    assert ext._interrupt.await_count == 1


@pytest.mark.asyncio
async def test_a_question_is_not_killed_by_the_tail_of_its_own_sentence():
    """Replayed from task_run.log 01:27:53-01:27:55.

    The question went to the model; 2.3 seconds later ' 加法' arrived and
    cancelled the turn before a character came back. Two of five questions
    died this way.
    """
    ext = make_extension()
    await ext._on_asr_result(
        _Result("那你告诉我如何教会六岁小孩加法", final=True)
    )
    ext._interrupt.reset_mock()

    for tail in (" 家", " 加法", " 家法"):
        await ext._on_asr_result(_Result(tail, final=False))

    assert ext._interrupt.await_count == 0


@pytest.mark.asyncio
async def test_the_gate_reopens_when_the_answer_has_been_heard():
    ext = make_extension()
    await ext._on_asr_result(_Result("問題", final=True))
    ext._interrupt_gate.answer_returned()  # what _on_llm_response does
    await ext._on_tts_speaking(_Speaking(True))
    await ext._on_tts_speaking(_Speaking(False))
    ext._interrupt.reset_mock()

    await ext._on_asr_result(_Result("下一句話", final=False))
    assert ext._interrupt.await_count == 1


@pytest.mark.asyncio
async def test_three_questions_in_a_row_all_reach_the_model():
    """Replayed from task_run.log 03:16:45-03:17:10: three questions went to
    the model and none was answered, each cancelled by the next."""
    ext = make_extension()

    for text in ("讲一个故事。", " 你还在吗？", " 你是谁？"):
        await ext._on_asr_result(_Result(text, final=True))

    assert ext.agent.queue_llm_input.await_count == 3
    # The first was interrupted by nothing; the two after it arrived while
    # the model was still thinking and must not have cancelled it.
    assert ext._interrupt.await_count == 1
