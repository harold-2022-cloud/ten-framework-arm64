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

from ten_ai_base.struct import (
    LLMResponseMessageDelta,
    LLMResponseMessageDone,
    LLMResponseReasoningDelta,
    LLMResponseReasoningDone,
)

from ambarella_llm2_python.ambarella import (
    AmbarellaChatClient,
    AmbarellaLLM2Config,
    _ReasoningSplitter,
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


# ---------------------------------------------------------------------------
# The board streams one character per SSE event and escapes whitespace, so
# </think>, <SP> and <NL> all arrive split across events. Measured on an
# N1-655 on 2026-09-14: 97 <SP> and 7 <NL> in one session, and a </think>
# spread over eight events. A parser that inspects each delta on its own sees
# none of them, and the reasoning reaches TTS as speech.
# ---------------------------------------------------------------------------


def split(events):
    sp, out = _ReasoningSplitter(), []
    for e in events:
        out += sp.feed(e)
    out += sp.flush()
    return (
        "".join(t for k, t in out if k == "reasoning"),
        "".join(t for k, t in out if k == "answer"),
    )


def test_the_shape_the_board_actually_sends():
    events = (
        list("Thinking")
        + ["<SP>"]
        + list("hard.")
        + ["<NL>"]
        + list("</think>")
        + ["<NL>", "<NL>"]
        + list("Hi")
        + ["<SP>"]
        + list("there")
    )
    assert split(events) == ("Thinking hard.\n", "\n\nHi there")


def test_escapes_split_character_by_character():
    events = list("Think") + list("<SP>") + list("</think>") + list("Answer")
    assert split(events) == ("Think ", "Answer")


def test_a_stream_without_the_tag_is_all_answer():
    # Withholding it would lose the reply outright.
    assert split(list("Just an answer")) == ("", "Just an answer")


def test_a_bare_angle_bracket_is_not_swallowed():
    assert split(list("a < b</think>ok")) == ("a < b", "ok")


def test_one_large_chunk():
    assert split(["Think<SP>x</think>Ans<NL>wer"]) == ("Think x", "Ans\nwer")


def test_the_tag_split_across_two_feeds():
    sp = _ReasoningSplitter()
    out = sp.feed("abc</thi") + sp.feed("nk>xyz") + sp.flush()
    assert "".join(t for k, t in out if k == "reasoning") == "abc"
    assert "".join(t for k, t in out if k == "answer") == "xyz"


def test_a_partial_tag_at_end_of_stream_is_emitted_not_lost():
    sp = _ReasoningSplitter()
    out = sp.feed("abc</thi") + sp.flush()
    assert "".join(t for _, t in out) == "abc</thi"


# ---------------------------------------------------------------------------
# End to end over the streaming path: main_control only speaks events whose
# type is "message" (extension.py:98,105), so reasoning has to leave under the
# reasoning types or it is read aloud.
# ---------------------------------------------------------------------------


def sse(events):
    """Frame a list of event payloads the way the board does."""
    return [("data: " + e + "\n\n").encode() for e in events]


async def collect(client, events):
    from ten_ai_base.struct import LLMRequest

    req = LLMRequest(
        request_id="t1",
        model="x",
        messages=[LLMMessageContent(role="user", content="hi")],
    )
    client._session = MagicMock()
    resp = _FakeResponse([b"".join(sse(events))])
    resp.status = 200

    class _Ctx:
        async def __aenter__(self):
            return resp

        async def __aexit__(self, *a):
            return False

    async def _text():
        return ""

    resp.text = _text
    client._session.post = MagicMock(return_value=_Ctx())
    client._ensure_session = AsyncMock()
    return [r async for r in client.get_chat_completions(req)]


@pytest.mark.asyncio
async def test_reasoning_leaves_under_the_reasoning_types():
    client = make_client()
    events = (
        list("Think")
        + ["<SP>"]
        + list("hard")
        + list("</think>")
        + list("Hi")
        + ["<SP>"]
        + list("there")
        + ["<DONE>"]
    )
    out = await collect(client, events)

    reasoning = [r for r in out if isinstance(r, LLMResponseReasoningDelta)]
    messages = [r for r in out if isinstance(r, LLMResponseMessageDelta)]
    assert reasoning, "the reasoning never left under its own type"
    assert "".join(r.delta for r in reasoning) == "Think hard"
    assert "".join(m.delta for m in messages) == "Hi there"
    assert any(isinstance(r, LLMResponseReasoningDone) for r in out)
    done = [r for r in out if isinstance(r, LLMResponseMessageDone)]
    assert done and done[-1].content == "Hi there"


@pytest.mark.asyncio
async def test_a_stream_with_no_tag_is_spoken_not_swallowed():
    client = make_client()
    out = await collect(client, list("Just an answer") + ["<DONE>"])
    messages = [r for r in out if isinstance(r, LLMResponseMessageDelta)]
    assert "".join(m.delta for m in messages) == "Just an answer"
    assert not [r for r in out if isinstance(r, LLMResponseReasoningDelta)]


@pytest.mark.asyncio
async def test_the_done_token_is_not_spoken():
    client = make_client()
    out = await collect(client, list("</think>") + list("Hi") + ["<DONE>"])
    spoken = "".join(
        m.delta for m in out if isinstance(m, LLMResponseMessageDelta)
    )
    assert "<DONE>" not in spoken and spoken == "Hi"
