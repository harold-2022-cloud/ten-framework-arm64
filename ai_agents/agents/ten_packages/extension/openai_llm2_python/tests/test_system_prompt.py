#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The system message: sent when there is a prompt, left out when there is
none. An empty system message is still a message the model reads; a model
evaluated with none -- Qwen3.5-4B for the meeting minutes -- must get none."""

import asyncio
from unittest.mock import MagicMock

from ten_ai_base.struct import LLMMessageContent, LLMRequest

from openai_llm2_python.openai import OpenAIChatGPT, OpenAILLM2Config


class Completions:
    """chat.completions, keeping each request; it answers with no chunks."""

    def __init__(self):
        self.requests = []

    async def create(self, **request):
        self.requests.append(request)

        async def nothing():
            for chunk in ():
                yield chunk

        return nothing()


def sent_messages(config_prompt, request_prompt=None):
    # As the extension builds it (extension.py): the class is a dataclass on
    # a pydantic model, so its own constructor wants every field.
    gpt = OpenAIChatGPT(
        MagicMock(),
        OpenAILLM2Config.model_validate(
            {
                "api_key": "EMPTY",
                "base_url": "http://127.0.0.1:8000/v1",
                "model": "qwen3.5_4B",
                "prompt": config_prompt,
            }
        ),
    )
    completions = Completions()
    gpt.client = MagicMock()
    gpt.client.chat.completions = completions
    request = LLMRequest(
        request_id="r1",
        messages=[LLMMessageContent(role="user", content="总结这段会议")],
        streaming=True,
        prompt=request_prompt,
    )

    async def ask():
        async for _ in gpt.get_chat_completions(request):
            pass

    asyncio.run(ask())
    return completions.requests[0]["messages"]


def test_no_prompt_sends_no_system_message():
    assert sent_messages("") == [{"role": "user", "content": "总结这段会议"}]


def test_a_configured_prompt_is_the_system_message():
    messages = sent_messages("Keep answers short.")
    assert messages[0] == {"role": "system", "content": "Keep answers short."}
    assert messages[1] == {"role": "user", "content": "总结这段会议"}


def test_a_request_prompt_wins_over_the_configured_one():
    messages = sent_messages(
        "Keep answers short.", request_prompt="用中文回答。"
    )
    assert messages[0] == {"role": "system", "content": "用中文回答。"}
