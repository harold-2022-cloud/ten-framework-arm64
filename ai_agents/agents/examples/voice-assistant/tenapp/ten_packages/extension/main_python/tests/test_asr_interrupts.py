#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""_on_asr_result has to consult the gate, not re-derive the rule itself."""

import asyncio
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
    # Each completed sentence replaces the one before it: the newest thing
    # the user said is what gets answered.
    assert ext._interrupt.await_count == 3


def test_the_speaking_event_can_actually_be_constructed():
    """The handler was tested with a stand-in; the event itself was not.

    AgentEventBase requires `type` and gives it no default (events.py:12).
    TTSSpeakingEvent did not set it, so every tts_audio_start and
    tts_audio_end raised a validation error that agent.py swallowed, and
    set_speaking was never once called on the board: every [gate] line in
    task_run.log of 2026-09-16 05:20 reads speaking=False.
    """
    from main_python.agent.events import TTSSpeakingEvent

    event = TTSSpeakingEvent(speaking=True)
    assert event.speaking is True
    assert event.name == "tts_speaking"


def test_every_agent_event_can_be_constructed_from_its_own_fields():
    """The same hole for anything added later."""
    import inspect

    from main_python.agent import events as ev
    from main_python.agent.events import AgentEventBase

    for name, cls in vars(ev).items():
        if (
            not inspect.isclass(cls)
            or not issubclass(cls, AgentEventBase)
            or cls is AgentEventBase
        ):
            continue
        required = [
            field
            for field, info in cls.model_fields.items()
            if info.is_required()
        ]
        assert "type" not in required, (
            f"{name} leaves `type` required, so constructing it raises and "
            "agent.py swallows the error"
        )


@pytest.mark.asyncio
async def test_the_same_question_arriving_twice_is_asked_once():
    """Replayed from task_run.log 05:22:40 and 05:22:42.

    Soniox finalised 讲一个笑话。 and, in the same millisecond, opened a new
    segment carrying the deferred remainder -- the same words with a leading
    space. It finalised 2.0 seconds later, cancelled the turn that was 2.0
    seconds into a reply needing 26 to 47, and the question was re-sent 3.9 ms
    after the cancellation, which the board refused.
    """
    ext = make_extension()

    await ext._on_asr_result(_Result("讲一个笑话。", final=True))
    ext._interrupt.reset_mock()
    ext.agent.queue_llm_input.reset_mock()

    await ext._on_asr_result(_Result(" 讲一个笑话。", final=True))

    assert ext._interrupt.await_count == 0, "the first turn was cancelled"
    assert ext.agent.queue_llm_input.await_count == 0, "it was asked twice"


@pytest.mark.asyncio
async def test_a_genuinely_new_question_still_gets_through():
    ext = make_extension()
    await ext._on_asr_result(_Result("讲一个笑话。", final=True))
    ext._interrupt.reset_mock()
    ext.agent.queue_llm_input.reset_mock()

    await ext._on_asr_result(_Result("那讲一个故事。", final=True))

    assert ext._interrupt.await_count == 1
    assert ext.agent.queue_llm_input.await_count == 1


@pytest.mark.asyncio
async def test_the_same_words_asked_again_later_are_a_new_question():
    """A user who repeats themselves after the answer means it."""
    ext = make_extension()
    await ext._on_asr_result(_Result("讲一个笑话。", final=True))
    ext._interrupt_gate.answer_returned()  # the turn finished
    ext._interrupt.reset_mock()
    ext.agent.queue_llm_input.reset_mock()

    await ext._on_asr_result(_Result("讲一个笑话。", final=True))

    assert ext.agent.queue_llm_input.await_count == 1


@pytest.mark.asyncio
async def test_the_whole_logged_failure_replayed():
    """Every ASR result of 2026-09-16 05:22:36-05:22:42, in order.

    What happened: one POST, cancelled 2.0 s in by the duplicate final, and a
    second POST 3.9 ms later that the board refused. One question asked, no
    answer heard.
    """
    ext = make_extension()
    observed = [
        ("讲一个笑话", False),
        ("讲一个笑话。", False),
        ("讲一个笑话。 讲一个笑话", False),
        ("讲一个笑话。 讲一个笑话。", False),
        ("讲一个笑话。", True),  # 05:22:40.825, segment 62b11757
        (" 讲一个笑话。", False),  # the deferred remainder, same words
        (" 讲一个笑话。", False),
        (" 讲一个笑话。", True),  # 05:22:42.845, segment 08a1448b
    ]
    for text, final in observed[:5]:
        await ext._on_asr_result(_Result(text, final=final))

    # The question is now with the model. Interrupts before this point cost
    # nothing -- LLMExec has no request to abort -- and the log shows ten of
    # them in this session doing exactly nothing.
    assert ext.agent.queue_llm_input.await_count == 1
    ext._interrupt.reset_mock()

    for text, final in observed[5:]:
        await ext._on_asr_result(_Result(text, final=final))

    assert ext._interrupt.await_count == 0, "the answer was cancelled"
    assert ext.agent.queue_llm_input.await_count == 1, "asked twice"


@pytest.mark.asyncio
async def test_a_stable_partial_reaches_the_model_when_asr_never_finalizes():
    """Replayed from task_run2.log 14:26:32-14:26:39.

    Soniox kept sending the complete user text as final=False, so the old
    control path waited forever and the LLM never saw "讲一个笑话".
    """
    ext = make_extension()
    ext.ASR_PARTIAL_COMMIT_DELAY_SECONDS = 0.01

    for text in (
        "讲",
        "讲一个",
        "讲一个笑",
        "讲一个笑话",
        "讲一个笑话",
    ):
        await ext._on_asr_result(_Result(text, final=False))

    await asyncio.sleep(0.02)

    ext.agent.queue_llm_input.assert_awaited_once_with("讲一个笑话")
    assert ext._send_transcript.call_args_list[-1].args == (
        "user",
        "讲一个笑话",
        True,
        123,
    )


@pytest.mark.asyncio
async def test_an_asr_final_cancels_the_stable_partial_fallback():
    ext = make_extension()
    ext.ASR_PARTIAL_COMMIT_DELAY_SECONDS = 0.05

    await ext._on_asr_result(_Result("讲一个笑话", final=False))
    await ext._on_asr_result(_Result("讲一个笑话。", final=True))
    await asyncio.sleep(0.06)

    ext.agent.queue_llm_input.assert_awaited_once_with("讲一个笑话。")
