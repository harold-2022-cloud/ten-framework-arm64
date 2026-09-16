#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Replay logged LLM text to verify what reaches TTS."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from main_python.agent.events import LLMResponseEvent
from main_python.extension import MainControlExtension
from main_python.helper import parse_sentences


LOGGED_ANSWER = (
    "\n\n老板问两个员工：“"
    "你们两个月的工资是多少？”"
    "一个员工回答：“这个月的，"
    "上个月的，"
    "还有下个月的，"
    "加起来是多少？”"
    "老板笑得直不起腰了。"
)

EXPECTED_TTS_SEGMENTS = [
    "老板问两个员工：“你们两个月的工资是多少？”",
    "一个员工回答：“这个月的，",
    "上个月的，",
    "还有下个月的，",
    "加起来是多少？”",
    "老板笑得直不起腰了。",
]


def replay_parse_sentences(text):
    fragment = ""
    segments = []
    for char in text:
        parsed, fragment = parse_sentences(fragment, char)
        segments.extend(parsed)
    if fragment.strip():
        segments.append(fragment.strip())
    return segments


def make_extension():
    ext = MainControlExtension("main_control")
    ext.ten_env = MagicMock()
    ext.ten_env.log_info = MagicMock()
    ext._send_to_tts = AsyncMock()
    ext._send_transcript = AsyncMock()
    return ext


def test_logged_answer_keeps_closing_quotes_with_the_sentence():
    """Replayed from /tmp/task_run.log 06:16:51-06:16:58."""

    assert replay_parse_sentences(LOGGED_ANSWER) == EXPECTED_TTS_SEGMENTS


@pytest.mark.asyncio
async def test_logged_answer_reaches_tts_without_empty_final_text():
    """The final event should carry the last sentence, not an empty close."""

    ext = make_extension()
    text_so_far = ""
    for char in LOGGED_ANSWER:
        text_so_far += char
        await ext._on_llm_response(
            LLMResponseEvent(
                delta=char,
                text=text_so_far,
                is_final=False,
            )
        )

    await ext._on_llm_response(
        LLMResponseEvent(
            delta="",
            text=LOGGED_ANSWER,
            is_final=True,
        )
    )

    calls = [
        (call.args[0], call.args[1])
        for call in ext._send_to_tts.call_args_list
    ]
    assert calls == [
        (segment, index == len(EXPECTED_TTS_SEGMENTS) - 1)
        for index, segment in enumerate(EXPECTED_TTS_SEGMENTS)
    ]
