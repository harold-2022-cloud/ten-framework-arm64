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
    # It goes out as reasoning first, because until the tag arrives that is
    # what it looks like, and is repeated as answer at the end so the reply is
    # spoken. The two land in different transcript channels, so the reader
    # sees a thought and then a reply, not the same line twice.
    reasoning, answer = split(list("Just an answer"))
    assert answer == "Just an answer"
    assert reasoning == "Just an answer"


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
    # Nothing is dropped: the truncated tag is text like any other, and with
    # no complete tag in the stream the whole thing is also spoken.
    assert "".join(t for k, t in out if k == "reasoning") == "abc</thi"
    assert "".join(t for k, t in out if k == "answer") == "abc</thi"


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
    # What matters is that it is spoken: main_control only speaks message
    # events. It is also shown as reasoning on the way past, which is the
    # price of showing the thinking as it happens.
    assert "".join(m.delta for m in messages) == "Just an answer"


@pytest.mark.asyncio
async def test_the_done_token_is_not_spoken():
    client = make_client()
    out = await collect(client, list("</think>") + list("Hi") + ["<DONE>"])
    spoken = "".join(
        m.delta for m in out if isinstance(m, LLMResponseMessageDelta)
    )
    assert "<DONE>" not in spoken and spoken == "Hi"


# --- Latency: the board re-processes its own history --------------------


def test_reset_every_turn_is_off_by_default():
    """Off, because it trades the conversation for speed."""
    from ambarella_llm2_python.ambarella import AmbarellaLLM2Config

    assert AmbarellaLLM2Config().reset_every_turn is False


def test_reset_every_turn_sends_reset_on_every_request():
    """Measured on the board: an 8-character query took 39 s with the history
    kept, against 8 s on the first turn, which resets. This makes that
    comparison a setting rather than a rebuild."""
    from ten_ai_base.struct import LLMRequest

    from ambarella_llm2_python.ambarella import (
        AmbarellaChatClient,
        AmbarellaLLM2Config,
    )

    class _Env:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    client = AmbarellaChatClient(
        _Env(), AmbarellaLLM2Config(reset_every_turn=True)
    )
    # The first turn resets anyway; the point is that the second still does.
    client._needs_reset = False  # pylint: disable=protected-access

    assert (
        client._reset_for_this_turn() is True
    )  # pylint: disable=protected-access
    assert (
        client._headers(True, client._reset_for_this_turn())[
            "Reset-En"
        ]  # pylint: disable=protected-access
        == "1"
    )


def test_history_is_kept_when_the_option_is_off():
    from ambarella_llm2_python.ambarella import (
        AmbarellaChatClient,
        AmbarellaLLM2Config,
    )

    class _Env:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    client = AmbarellaChatClient(_Env(), AmbarellaLLM2Config())
    client._needs_reset = False  # pylint: disable=protected-access

    assert (
        client._reset_for_this_turn() is False
    )  # pylint: disable=protected-access


# --- A pinned framing must not discard the answer in silence -------------


def _fake_response(chunks):
    class _Content:
        async def iter_any(self):
            for chunk in chunks:
                yield chunk

    class _Resp:
        content = _Content()

    return _Resp()


def _client(**overrides):
    from ambarella_llm2_python.ambarella import (
        AmbarellaChatClient,
        AmbarellaLLM2Config,
    )

    class _Env:
        def __init__(self):
            self.lines = []

        def __getattr__(self, _name):
            return lambda *a, **k: self.lines.append(a[0] if a else "")

    env = _Env()
    return AmbarellaChatClient(env, AmbarellaLLM2Config(**overrides)), env


# What the board actually returned, read off the socket on 2026-09-15:
# Content-Type said text/event-stream and the body carried no 'data:' framing.
UNFRAMED = [
    b'Alright, the user said "Hello." ',
    "你好！有什么可以帮你的吗？\n".encode(),
]


@pytest.mark.asyncio
async def test_an_unframed_body_is_not_discarded_when_sse_is_pinned():
    """Pinned to sse, every line without 'data:' was dropped by a continue,
    and the turn ended with no text, no error and no log line."""
    client, _ = _client(response_format="sse")
    out = [text async for text in client._iter_deltas(_fake_response(UNFRAMED))]

    assert "".join(out).strip(), "the whole answer was swallowed"


@pytest.mark.asyncio
async def test_a_framing_mismatch_is_reported():
    client, env = _client(response_format="sse")
    async for _ in client._iter_deltas(_fake_response(UNFRAMED)):
        pass

    assert any(
        "framing" in str(line).lower() for line in env.lines
    ), f"nothing said the framing did not match: {env.lines}"


@pytest.mark.asyncio
async def test_a_properly_framed_sse_body_is_unaffected():
    client, _ = _client(response_format="sse")
    framed = [b"data: hello\n", b"data:  world\n", b"data: <DONE>\n"]
    out = [text async for text in client._iter_deltas(_fake_response(framed))]

    assert "".join(out) == "hello world"


# --- Reasoning is shown as it arrives, not held to the end ---------------


def _splitter():
    from ambarella_llm2_python.ambarella import _ReasoningSplitter

    return _ReasoningSplitter()


def test_reasoning_is_emitted_as_it_arrives():
    """The board's own reference adapter does this.

    openai_llm2_python/think_parser.py emits reasoning_delta before the
    closing tag arrives. Holding it meant the transcript showed nothing for
    the whole thinking phase -- 45 seconds on this board -- and a user who
    sees nothing assumes they were not heard and speaks again, which cancels
    the turn.
    """
    s = _splitter()
    out = s.feed("我先想一下")

    assert out == [("reasoning", "我先想一下")]


def test_reasoning_arrives_in_pieces_as_the_stream_does():
    s = _splitter()
    first = s.feed("讓我")
    second = s.feed("想想")

    assert first == [("reasoning", "讓我")]
    assert second == [("reasoning", "想想")]


def test_the_answer_still_follows_the_closing_tag():
    s = _splitter()
    pairs = s.feed("想完了</think>答案在這")

    assert pairs == [("reasoning", "想完了"), ("answer", "答案在這")]


def test_a_stream_with_no_closing_tag_is_still_spoken():
    """Nothing marked it as reasoning, so it was the answer all along.

    Emitting it as reasoning on the way past would leave the answer empty and
    the assistant silent, which is worse than showing the text twice.
    """
    s = _splitter()
    shown = s.feed("這其實是答案")
    assert shown == [("reasoning", "這其實是答案")]

    assert s.flush() == [("answer", "這其實是答案")]


def test_flush_after_a_closing_tag_does_not_repeat_the_answer():
    s = _splitter()
    s.feed("想法</think>答案")

    assert s.flush() == []


def test_a_token_split_across_events_is_still_recognised():
    """The board sends one character per SSE event, so every token arrives
    in pieces."""
    s = _splitter()
    out = []
    for char in "想</think>答":
        out += s.feed(char)

    assert out == [("reasoning", "想"), ("answer", "答")]


def test_escapes_inside_reasoning_are_decoded():
    s = _splitter()
    assert s.feed("一<SP>二") == [
        ("reasoning", "一"),
        ("reasoning", " "),
        ("reasoning", "二"),
    ]


@pytest.mark.asyncio
async def test_reasoning_is_done_once_not_once_per_character():
    """Measured on the board: 262 reasoning deltas and 262 reasoning dones.

    main_control turns every Done into a transcript marked final
    (extension.py:128-133), so the display received 262 completed thoughts
    for one. Done means the thinking ended, which happens once, at the tag.
    """
    client = make_client()
    out = await collect(client, list("想了很久</think>答案") + ["<DONE>"])

    deltas = [r for r in out if isinstance(r, LLMResponseReasoningDelta)]
    dones = [r for r in out if isinstance(r, LLMResponseReasoningDone)]

    assert len(deltas) > 1, "reasoning should still stream"
    assert len(dones) == 1
    assert dones[0].content == "想了很久"


@pytest.mark.asyncio
async def test_reasoning_is_done_when_the_stream_ends_without_the_tag():
    """No tag ever came, so the thinking ended when the stream did."""
    client = make_client()
    out = await collect(client, list("沒有標籤") + ["<DONE>"])

    dones = [r for r in out if isinstance(r, LLMResponseReasoningDone)]
    assert len(dones) == 1


@pytest.mark.asyncio
async def test_a_bypassed_think_block_speaks_as_it_arrives():
    """Replayed from the board on 2026-09-17, after Ambarella's bypass.

    Closing the think block in prompt_config.json means no </think> ever
    comes. Left to assume generation starts inside the block, the splitter
    called the whole reply reasoning -- which main_control renders and never
    speaks -- and flush() repeated it as the answer only once the stream
    ended: 70 characters in one piece at 7.5 s, where a first sentence should
    have reached TTS at about one second.
    """
    client = make_client(starts_in_reasoning=False)
    out = await collect(
        client, list("在一个小村庄里，有一个木匠。") + ["<DONE>"]
    )

    messages = [r for r in out if isinstance(r, LLMResponseMessageDelta)]
    assert "".join(m.delta for m in messages) == "在一个小村庄里，有一个木匠。"
    # Spoken as it arrives, not gathered up and released at the end.
    assert len(messages) > 1, "the whole reply arrived in one piece"
    # And none of it was announced as thinking.
    assert not [r for r in out if isinstance(r, LLMResponseReasoningDelta)]


@pytest.mark.asyncio
async def test_the_closing_tag_still_works_when_the_block_is_bypassed():
    """The option says where generation starts, not that tags are ignored."""
    client = make_client(starts_in_reasoning=False)
    out = await collect(
        client, list("plain") + list("</think>") + list("more") + ["<DONE>"]
    )
    messages = [r for r in out if isinstance(r, LLMResponseMessageDelta)]
    assert "".join(m.delta for m in messages) == "plainmore"


@pytest.mark.asyncio
async def test_the_block_is_assumed_open_unless_told_otherwise():
    """The board ships with an unclosed <think> in Symbol1; default to that."""
    assert AmbarellaLLM2Config().starts_in_reasoning is True
    client = make_client()
    out = await collect(client, list("thinking") + ["<DONE>"])
    assert [r for r in out if isinstance(r, LLMResponseReasoningDelta)]
