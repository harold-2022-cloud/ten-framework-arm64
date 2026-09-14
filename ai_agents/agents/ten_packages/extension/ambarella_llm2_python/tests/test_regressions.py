#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Regressions for defects found reviewing this extension.

Each test here fails against the code as it stood before the fix it
covers; none of them needs a network or a board.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ten_ai_base.struct import LLMMessageContent

from ambarella_llm2_python.ambarella import (
    AmbarellaChatClient,
    AmbarellaLLM2Config,
    _resolve_session_id,
    _sse_payload,
    _strip_reasoning,
)
from ambarella_llm2_python.extension import AmbarellaLLM2Extension


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, chunks):
        self.content = _FakeContent(chunks)


def make_client(**overrides):
    config = AmbarellaLLM2Config(**overrides)
    env = AsyncMock()
    env.log_info = MagicMock()
    env.log_warn = MagicMock()
    env.log_error = MagicMock()
    env.log_debug = MagicMock()
    return AmbarellaChatClient(env, config)


async def drain(client, chunks):
    return [text async for text in client._iter_deltas(_FakeResponse(chunks))]


@pytest.mark.asyncio
async def test_multibyte_text_survives_a_chunk_split():
    """A UTF-8 character split across two TCP chunks must not corrupt.

    iter_any() yields arbitrary chunk boundaries. Decoding each chunk on
    its own turned every straddling CJK character into U+FFFD silently --
    the failure mode that matters most for a Chinese-language assistant.
    """
    payload = "你好世界".encode("utf-8")
    client = make_client(response_format="raw")
    # Split in the middle of the second character's 3-byte sequence.
    text = "".join(await drain(client, [payload[:4], payload[4:]]))
    assert text == "你好世界"
    assert "�" not in text


@pytest.mark.asyncio
async def test_sse_payload_keeps_whitespace_inside_a_token():
    """Stripping the whole payload glues streamed words together."""
    assert _sse_payload("data: hello ") == "hello "
    assert _sse_payload("data:hello") == "hello"
    # Only ONE leading space is the field separator, per the SSE spec.
    assert _sse_payload("data:  indented") == " indented"


@pytest.mark.asyncio
async def test_streamed_words_are_not_glued_together():
    client = make_client(response_format="sse")
    chunks = [b"data: Hello \n", b"data: world \n", b"data: [DONE]\n"]
    assert "".join(await drain(client, chunks)) == "Hello world "


@pytest.mark.asyncio
async def test_a_short_first_chunk_does_not_mislock_the_mode():
    """Sniffing on a 2-byte first chunk would pick 'raw' for an SSE stream."""
    client = make_client(response_format="auto")
    chunks = [b"da", b'ta: {"delta": "hi"}\n', b"data: [DONE]\n"]
    assert "".join(await drain(client, chunks)) == "hi"


@pytest.mark.asyncio
async def test_a_reset_after_abort_re_sends_the_system_prompt():
    """Reset-En clears the board history, which holds the folded-in prompt.

    Drives the real abort path: a barge-in closes the generator, the
    except handler runs, and the next turn must fold the prompt in again.
    Leaving _prompt_sent latched meant the session ran promptless from
    that point on, with nothing logged to say so.
    """
    client = make_client(reset_after_abort=True, prompt="you are helpful")

    class _Ctx:
        async def __aenter__(self):
            resp = _FakeResponse([b"data: hi\n"])
            resp.status = 200
            return resp

        async def __aexit__(self, *exc):
            return False

    session = MagicMock()
    session.post = MagicMock(return_value=_Ctx())
    client._session = session
    client._ensure_session = AsyncMock()

    message = MagicMock(spec=LLMMessageContent)
    message.role = "user"
    message.content = "hello"
    request = MagicMock()
    request.messages = [message]
    request.tools = None
    request.prompt = ""
    request.streaming = True

    gen = client.get_chat_completions(request)
    await gen.__anext__()
    assert client._prompt_sent is True
    await gen.aclose()

    assert client._needs_reset is True, "abort must schedule the reset"
    assert (
        client._prompt_sent is False
    ), "a reset wipes the prompt, so it must be re-sent"


@pytest.mark.asyncio
async def test_uninitialised_client_reports_why():
    """on_start returns early on bad config, leaving self.client as None."""
    extension = AmbarellaLLM2Extension("test")
    env = AsyncMock()
    env.log_debug = MagicMock()
    with pytest.raises(RuntimeError, match="not initialized"):
        extension.on_call_chat_completion(env, MagicMock())


# ---------------------------------------------------------------------------
# Session-Id has to be a decimal integer.
#
# The board parses the header numerically. A hex id such as uuid4().hex either
# leads with a letter and parses to zero -- the server then logs
# "session_id=0 should not be 0" and closes the connection without sending any
# HTTP response, which curl reports as (52) Empty reply from server -- or
# leads with a digit and is silently truncated at the first letter, which
# collides across extension instances. Observed on an N1-655 on 2026-09-14.
# ---------------------------------------------------------------------------


def test_generated_session_id_is_a_non_zero_decimal_integer():
    for _ in range(200):
        generated = _resolve_session_id("")
        assert generated.isdigit(), f"not decimal: {generated!r}"
        assert int(generated) > 0, f"zero session id: {generated!r}"


def test_configured_numeric_session_id_is_used_verbatim():
    assert _resolve_session_id("1234") == "1234"
    assert _resolve_session_id("  1234  ") == "1234"


@pytest.mark.parametrize(
    "bad", ["0", "00", "af2a9c1b7e4d0856", "12ab", "diag1", "-5", "1.5"]
)
def test_non_integer_session_id_is_rejected(bad):
    with pytest.raises(ValueError, match="non-zero decimal integer"):
        _resolve_session_id(bad)


def test_client_sends_a_decimal_session_id_header():
    client = make_client()
    headers = client._headers(streaming=True, reset=False)
    assert headers["Session-Id"].isdigit()
    assert int(headers["Session-Id"]) > 0


# ---------------------------------------------------------------------------
# deepseek_7B is an R1 distill and reasons before answering. A non-streaming
# reply measured on an N1-655 on 2026-09-14 is "<reasoning>\n</think>\n\n
# <answer>" -- the closing tag with no opening one, because generation starts
# already inside the block. Without stripping it, the board's internal
# monologue is what reaches TTS.
# ---------------------------------------------------------------------------


def test_reasoning_before_the_closing_tag_is_dropped():
    board = (
        'Alright, the user said "Hello." I should respond warmly.\n'
        "</think>\n\nHello! How can I assist you today?"
    )
    assert _strip_reasoning(board) == "Hello! How can I assist you today?"


def test_a_reply_without_the_tag_is_kept_whole():
    # Absent the delimiter nothing marks the text as reasoning, and dropping
    # it would lose the answer outright.
    assert _strip_reasoning("  Hello there.  ") == "Hello there."


def test_only_the_text_after_the_delimiter_survives():
    assert _strip_reasoning("a</think>b") == "b"


def test_an_empty_answer_after_the_tag_yields_empty():
    assert _strip_reasoning("thinking only</think>   ") == ""
