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
