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
