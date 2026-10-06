#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""One question to the board's LLM, one answer back, nothing remembered.

The fake environment replays responses built with ten_ai_base's own models,
so the JSON parsed here is the JSON the llm extension sends.
"""

import asyncio
import json

import pytest
from ten_ai_base.struct import (
    LLMResponseMessageDelta,
    LLMResponseMessageDone,
    LLMResponseReasoningDelta,
    LLMResponseReasoningDone,
)
from ten_runtime import StatusCode

from meeting_control_python.llm import LLMClient


class Result:
    def __init__(self, payload="", final=False, status=StatusCode.OK):
        self.payload, self.final, self.status = payload, final, status

    def get_status_code(self):
        return self.status

    def is_final(self):
        return self.final

    def get_property_to_json(self, _path=None):
        return self.payload, None


class Env:
    """Replays a script per question and keeps every command it was sent."""

    def __init__(self, *scripts, hang=False):
        self.scripts = list(scripts)
        self.hang = hang
        self.sent = []
        self.errors = []

    async def send_cmd_ex(self, cmd):
        payload, _ = cmd.get_property_to_json(None)
        self.sent.append((cmd.get_name(), json.loads(payload)))
        if self.hang:
            await asyncio.sleep(3600)
        for result in self.scripts.pop(0):
            yield result, None

    async def send_cmd(self, cmd):
        payload, _ = cmd.get_property_to_json(None)
        self.sent.append((cmd.get_name(), json.loads(payload)))
        return Result(final=True), None

    def log_info(self, *_a, **_k):
        pass

    log_warn = log_debug = log_info

    def log_error(self, message, **_k):
        self.errors.append(message)


def delta(text):
    return Result(
        LLMResponseMessageDelta(
            response_id="r", role="assistant", content=text, delta=text[-1:]
        ).model_dump_json()
    )


def done(text):
    return Result(
        LLMResponseMessageDone(
            response_id="r", role="assistant", content=text
        ).model_dump_json()
    )


def end():
    return Result(final=True)


@pytest.mark.asyncio
async def test_the_answer_is_the_finished_message():
    env = Env([delta("結"), delta("結論"), done("結論：下週出版本。"), end()])

    assert await LLMClient(env).ask("問題") == "結論：下週出版本。"


@pytest.mark.asyncio
async def test_each_question_goes_alone_with_nothing_before_it():
    env = Env([done("一"), end()], [done("二"), end()])
    client = LLMClient(env)

    await client.ask("第一題")
    await client.ask("第二題")

    name, request = env.sent[1]
    assert name == "chat_completion"
    assert [m["content"] for m in request["messages"]] == ["第二題"]


@pytest.mark.asyncio
async def test_reasoning_is_not_the_answer():
    thinking = LLMResponseReasoningDelta(
        response_id="r", role="assistant", content="想一想", delta="想"
    ).model_dump_json()
    thought = LLMResponseReasoningDone(
        response_id="r", role="assistant", content="想一想"
    ).model_dump_json()
    env = Env([Result(thinking), Result(thought), done("答案"), end()])

    assert await LLMClient(env).ask("問題") == "答案"


@pytest.mark.asyncio
async def test_a_turn_past_its_time_is_aborted_and_answers_nothing():
    env = Env(hang=True)

    answer = await LLMClient(env, timeout_s=0.05).ask("問題")

    assert answer == ""
    (_, asked), (abort_name, abort) = env.sent
    assert abort_name == "abort"
    assert abort["request_id"] == asked["request_id"]


@pytest.mark.asyncio
async def test_an_error_from_the_llm_answers_nothing_and_says_so():
    env = Env(
        [
            Result(
                '{"message": "board busy"}', final=True, status=StatusCode.ERROR
            )
        ]
    )

    assert await LLMClient(env).ask("問題") == ""
    assert env.errors
