#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
# ------------------------------
# Config
# ------------------------------
import asyncio
import codecs
import json
import random
import time
import uuid
from typing import AsyncGenerator, Optional

import aiohttp
from pydantic import BaseModel
from ten_ai_base.struct import (
    LLMMessageContent,
    LLMRequest,
    LLMResponse,
    LLMResponseMessageDelta,
    LLMResponseMessageDone,
    LLMResponseReasoningDelta,
    LLMResponseReasoningDone,
)
from ten_runtime import AsyncTenEnv

# The runtime's own category for lines a human reads when something is slow.
LOG_CATEGORY_KEY_POINT = "key_point"
LOG_CATEGORY_VENDOR = "vendor"

SSE_PREFIX = "data:"
# The board terminates with <DONE>; [DONE] is the convention elsewhere and is
# accepted too, since nothing documents which one to expect.
SSE_DONE_TOKENS = ("<DONE>", "[DONE]")

# The board emits only the closing tag; generation starts inside the block.
CLOSE_THINK_TAG = "</think>"

# Whitespace is escaped, because an SSE value cannot carry a leading or
# trailing space safely. Measured on an N1-655 on 2026-09-14: 97 <SP> and 7
# <NL> events in a single session.
ESCAPES = {"<SP>": " ", "<NL>": "\n"}

# Every token the board can emit starts with "<", so a partial one can only
# ever be a suffix of the text seen so far that begins at the last "<".
_TOKENS = tuple(ESCAPES) + (CLOSE_THINK_TAG,) + SSE_DONE_TOKENS

# Keys a streamed Ambarella payload might carry the assistant text under.
# The board's HTTP interface is documented only by one curl example and its
# headers -- the response framing is not specified anywhere, so this list is
# a best guess. See the "Response framing" section of README.md.
TEXT_KEYS = ("delta", "answer", "text", "content", "response", "token")


def _sse_payload(line: str) -> str:
    """Strip the SSE field prefix, preserving whitespace inside the value.

    The spec removes exactly one optional space after the colon. Stripping
    the whole payload would delete the leading and trailing spaces of a
    plain-text token, gluing words together in the assembled answer.
    """
    payload = line[len(SSE_PREFIX) :]
    if payload.startswith(" "):
        payload = payload[1:]
    return payload


def _strip_reasoning(text: str) -> str:
    """
    Drop the model's chain-of-thought.

    deepseek_7B is an R1 distill and reasons before it answers. Measured on an
    N1-655 on 2026-09-14, a non-streaming reply is

        <reasoning>\n</think>\n\n<answer>

    -- the closing tag with no opening one, because generation starts already
    inside the block. Streaming replies carry no reasoning at all, so this only
    ever fires when the caller turned streaming off; without it the board's
    internal monologue is what reaches TTS.

    A reply with no closing tag is returned whole: absent the delimiter there
    is nothing to say the text is reasoning, and swallowing it would lose the
    answer outright.
    """
    _, delimiter, answer = text.partition(CLOSE_THINK_TAG)
    return (answer if delimiter else text).strip()


class _ReasoningSplitter:
    """
    Turn the board's escaped, character-at-a-time stream into (kind, text)
    pairs, where kind is "reasoning" before the closing think tag and "answer"
    after it.

    Every token the board emits -- <SP>, <NL>, </think> -- can be split across
    SSE events, since each event carries one character. Anything that might
    still become a token is held back until it either completes or is ruled
    out, which costs at most len("</think>") - 1 characters of lag.

    Reasoning is withheld rather than emitted as it arrives, because until the
    tag appears there is no way to know the text is reasoning at all. A stream
    that ends without one is emitted as answer in full: classifying it as
    reasoning would drop the reply, since main_control only speaks answers.
    Reasoning is for display, so releasing it in one piece costs nothing.
    """

    def __init__(self) -> None:
        self._pending = ""  # may still grow into a token
        self._held = ""  # decoded text whose kind is not yet decided
        self._in_reasoning = True  # generation starts inside the block

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self._pending += chunk
        out: list[tuple[str, str]] = []
        while self._pending:
            start = self._pending.find("<")
            if start < 0:
                out += self._take(self._pending)
                self._pending = ""
                break
            if start > 0:
                out += self._take(self._pending[:start])
                self._pending = self._pending[start:]

            token = next(
                (t for t in _TOKENS if self._pending.startswith(t)), None
            )
            if token is None:
                if any(t.startswith(self._pending) for t in _TOKENS):
                    break  # still might become a token
                out += self._take("<")  # ruled out: a literal "<"
                self._pending = self._pending[1:]
                continue

            self._pending = self._pending[len(token) :]
            if token == CLOSE_THINK_TAG:
                self._in_reasoning = False
                if self._held:
                    out.append(("reasoning", self._held))
                    self._held = ""
            elif token in ESCAPES:
                out += self._take(ESCAPES[token])
            # a terminator token yields nothing

        return [(k, t) for k, t in out if t]

    def flush(self) -> list[tuple[str, str]]:
        """
        Close the stream. Text still held when no tag ever arrived is the
        answer, not reasoning.
        """
        tail, self._held, self._pending = (
            self._held + self._pending,
            "",
            "",
        )
        return [(self._kind(), tail)] if tail else []

    def _take(self, text: str) -> list[tuple[str, str]]:
        if self._in_reasoning:
            self._held += text  # kind still undecided
            return []
        return [("answer", text)]

    def _kind(self) -> str:
        # Reaching the end still "in reasoning" means no tag was ever sent,
        # so the text was the answer all along.
        return "answer"


def _resolve_session_id(configured: str) -> str:
    """
    The board parses Session-Id as an integer, so it has to be decimal digits
    and must not come out as zero. A hex id such as uuid4().hex is either
    rejected outright (leading letter -> 0) or silently truncated at the first
    letter, which collides across instances.
    """
    candidate = (configured or "").strip()
    if candidate:
        if not candidate.isdigit() or int(candidate) == 0:
            raise ValueError(
                "session_id must be a non-zero decimal integer, as the board "
                f"parses it numerically; got {candidate!r}"
            )
        return candidate
    # Positive and inside int32, which is what the board's demo server uses.
    return str(random.randint(1, 2**31 - 1))


class AmbarellaLLM2Config(BaseModel):
    # The on-board LLM demo server, started by run_llm_demo.sh. Point this at
    # the board's IP when the agent runs off-board.
    base_url: str = "http://127.0.0.1:8080"
    # model_type as accepted by run_llm_demo.sh. 9 is deepseek_7B, the only
    # model the developer kit guide lists as pre-converted for the N1-655.
    model_type: int = 9
    # Reused across turns so the board keeps the conversation history. Left
    # empty, a random one is generated per extension instance.
    #
    # Must be a non-zero DECIMAL INTEGER. The board parses this header
    # numerically and refuses a zero -- a non-numeric value logs
    # "session_id=0 should not be 0" in /tmp/log.txt and the connection is
    # closed with no HTTP response at all.
    session_id: str = ""
    # Folded into the first query, as the interface has no system role.
    prompt: str = ""
    # "auto" sniffs SSE vs raw text from the first chunk. Pin it to "sse" or
    # "raw" once the board's actual framing has been observed.
    response_format: str = "auto"
    # Upper bound on streaming; the per-request value still wins.
    streaming: bool = True
    # Clear the board-side history on this instance's first request, so a
    # previous worker's session cannot leak into this one.
    reset_on_first_request: bool = True
    # The board keeps history server-side and cannot rewind a turn it already
    # half-answered. Enabling this trades the whole history for a clean
    # context after a barge-in; off by default, because barge-in is routine
    # in a voice pipeline and wiping history on each one is worse.
    reset_after_abort: bool = False
    # Send Reset-En on every turn, so the board answers the question rather
    # than re-reading the conversation that led to it. Measured on an N1-655
    # on 2026-09-15: the first turn, which resets, answered in 8 s; the two
    # after it, with the history kept, took 39 s and 28 s for an eight-
    # character question. The board re-processes its own history and there is
    # no way to ask it not to except by clearing it.
    #
    # Off by default: this buys latency with the conversation itself. The
    # assistant stops being able to answer "and what about tomorrow?".
    reset_every_turn: bool = False
    # Networking. The total timeout is generous: a 7B model at W4A16 on
    # CVflow has no published token rate, and the first load after boot can
    # take up to 80 s.
    connect_timeout_s: float = 15.0
    total_timeout_s: float = 120.0


# ------------------------------
# Thin Ambarella streaming client
# ------------------------------
class AmbarellaChatClient:
    def __init__(self, ten_env: AsyncTenEnv, config: AmbarellaLLM2Config):
        self.ten_env = ten_env
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_id = _resolve_session_id(config.session_id)
        self._needs_reset = config.reset_on_first_request
        self._prompt_sent = False
        self._warned_tools = False
        # run_llm_demo.sh is documented with --max_user 1, so two overlapping
        # turns would contend for the board's single slot. Drop this lock if
        # the server is started with a higher --max_user.
        self._turn_lock = asyncio.Lock()

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                connect=self.config.connect_timeout_s,
                total=self.config.total_timeout_s,
            )
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def aclose(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _url(self) -> str:
        # The guide posts to the server root:
        #   curl -X POST --url http://127.0.0.1:8080/
        return self.config.base_url.rstrip("/") + "/"

    def _reset_for_this_turn(self) -> bool:
        """Whether this request clears the board-side history."""
        return self._needs_reset or self.config.reset_every_turn

    def _headers(self, streaming: bool, reset: bool) -> dict:
        return {
            "Session-Id": self._session_id,
            "Model-Type": str(self.config.model_type),
            "Stream-Off": "0" if streaming else "1",
            "Reset-En": "1" if reset else "0",
            "Content-Type": "text/plain; charset=utf-8",
        }

    def _latest_user_text(self, request_input: LLMRequest) -> str:
        """
        The board takes a single prompt string and keeps the history itself,
        keyed by Session-Id, so only the newest user turn is sent -- the same
        trade the Dify extension makes with its conversation_id.
        """
        for message in reversed(request_input.messages or []):
            if not isinstance(message, LLMMessageContent):
                continue
            if message.role != "user":
                continue
            if isinstance(message.content, str):
                return message.content
            if isinstance(message.content, list):
                chunks = [
                    getattr(item, "text", "")
                    for item in message.content
                    if hasattr(item, "text")
                ]
                joined = "\n".join(c for c in chunks if c)
                if joined:
                    return joined

        # Fall back to the last text-looking message of any role.
        for message in reversed(request_input.messages or []):
            if isinstance(message, LLMMessageContent) and isinstance(
                message.content, str
            ):
                return message.content
        return ""

    def _extract_text(self, payload: str) -> str:
        """Pull the assistant text out of one streamed payload."""
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            # Not JSON -- the payload is the text itself.
            return payload
        if not isinstance(event, dict):
            return payload
        for key in TEXT_KEYS:
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    async def _iter_deltas(
        self, resp: aiohttp.ClientResponse
    ) -> AsyncGenerator[str, None]:
        """
        Yield assistant text fragments off the wire.

        Handles both framings the board might use: SSE-style "data:" lines,
        and an unframed stream of UTF-8 text.
        """
        mode = self.config.response_format
        buffer = ""
        # A pinned framing is a claim about the server, and the server can
        # stop honouring it. Every line that does not match used to be
        # dropped by the continue below, so a body in the other framing
        # produced no text, no error and no log line -- the turn simply had
        # nothing in it. Track whether anything matched, and say so.
        sse_lines_seen = 0
        unmatched_lines = 0
        # iter_any() yields arbitrary TCP chunks, so a multi-byte character
        # can straddle two of them. A per-chunk bytes.decode() would turn
        # every split CJK character into U+FFFD, silently: the stream is
        # decoded incrementally instead, holding partial sequences back.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        async for chunk in resp.content.iter_any():
            if not chunk:
                continue
            buffer += decoder.decode(chunk)

            if mode == "auto":
                # Wait for enough bytes to tell "data:" from a payload that
                # merely starts with "da"; a short first chunk would
                # otherwise lock the stream into the wrong mode for good.
                stripped = buffer.lstrip()
                if len(stripped) < len(SSE_PREFIX):
                    continue
                mode = "sse" if stripped.startswith(SSE_PREFIX) else "raw"
                self.ten_env.log_info(
                    f"[Ambarella] sniffed response_format={mode}"
                )

            if mode == "raw":
                yield buffer
                buffer = ""
                continue

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r\n")
                if not line.startswith(SSE_PREFIX):
                    # A blank line separates SSE events and carries nothing;
                    # only a non-empty line that is not an event is evidence
                    # of the wrong framing.
                    if line.strip():
                        unmatched_lines += 1
                        if sse_lines_seen == 0:
                            # Nothing has parsed as SSE, so this is not a
                            # stray comment inside a stream -- it is the
                            # whole body in another framing. Switch rather
                            # than discard it.
                            self.ten_env.log_error(
                                "[Ambarella] framing mismatch: pinned "
                                f"response_format={self.config.response_format}"
                                " but the body has no 'data:' prefix; "
                                "reading it as raw text instead. Pin "
                                'response_format to "raw" if this persists.',
                                category=LOG_CATEGORY_VENDOR,
                            )
                            mode = "raw"
                            buffer = line + "\n" + buffer
                            break
                    continue
                sse_lines_seen += 1
                payload = _sse_payload(line)
                if payload.strip() in SSE_DONE_TOKENS:
                    return
                text = self._extract_text(payload)
                if text:
                    yield text

        buffer += decoder.decode(b"", final=True)

        # Whatever is left had no trailing newline to close it.
        if not buffer:
            return
        if mode == "raw":
            yield buffer
            return
        tail = buffer.rstrip("\r\n")
        if not tail.startswith(SSE_PREFIX):
            if tail.strip() and sse_lines_seen == 0:
                # A body with no newline at all, in the other framing.
                self.ten_env.log_error(
                    "[Ambarella] framing mismatch: pinned "
                    f"response_format={self.config.response_format} but the "
                    "body has no 'data:' prefix; reading it as raw text.",
                    category=LOG_CATEGORY_VENDOR,
                )
                yield tail
            return
        payload = _sse_payload(tail)
        if payload.strip() and payload.strip() not in SSE_DONE_TOKENS:
            text = self._extract_text(payload)
            if text:
                yield text

    async def get_chat_completions(
        self, request_input: LLMRequest
    ) -> AsyncGenerator[LLMResponse, None]:
        """
        Map LLMRequest -> the Ambarella LLM demo HTTP interface.

        Emit LLMResponseMessageDelta and LLMResponseMessageDone, mirroring
        the OpenAI and Dify LLM2 extensions.
        """
        if request_input.tools and not self._warned_tools:
            self._warned_tools = True
            self.ten_env.log_warn(
                f"[Ambarella] {len(request_input.tools)} tool(s) registered, "
                "but the board's LLM HTTP interface takes a plain-text prompt "
                "and has no tool-calling support; they will be ignored and no "
                "tool call will ever be emitted."
            )

        created = int(time.time())
        response_id = f"{self._session_id}-{uuid.uuid4().hex[:8]}"

        query = self._latest_user_text(request_input)
        if not query:
            self.ten_env.log_warn(
                "[Ambarella] no user text in request, nothing to send"
            )
            yield LLMResponseMessageDone(
                response_id=response_id,
                role="assistant",
                content="",
                created=created,
            )
            return

        system_prompt = request_input.prompt or self.config.prompt
        if system_prompt and not self._prompt_sent:
            # No system role on this interface -- fold the prompt into the
            # first turn and let the board carry it in session history.
            query = f"{system_prompt}\n\n{query}"

        streaming = self.config.streaming and request_input.streaming
        reset = self._reset_for_this_turn()

        await self._ensure_session()
        assert self._session is not None

        full_content = ""
        # How long the board takes is the pipeline's dominant cost and is not
        # visible anywhere else: the metrics the base class emits are the
        # TTS's. Logging it here saves correlating timestamps across three
        # extensions to answer "why was that slow".
        started = time.monotonic()
        first_delta_at: Optional[float] = None
        async with self._turn_lock:
            self.ten_env.log_info(
                f"[Ambarella] POST {self._url()} "
                f"model_type={self.config.model_type} "
                f"session_id={self._session_id} "
                f"streaming={streaming} reset={reset} "
                f"query_len={len(query)}"
            )
            try:
                async with self._session.post(
                    self._url(),
                    data=query.encode("utf-8"),
                    headers=self._headers(streaming, reset),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(
                            "Ambarella LLM request failed: "
                            f"status={resp.status} body={body[:512]}"
                        )

                    # The turn reached the model, so the board-side history is
                    # ours now; do not clear it again on the next turn.
                    self._needs_reset = False
                    self._prompt_sent = True

                    if not streaming:
                        full_content = _strip_reasoning(await resp.text())
                        if full_content:
                            yield LLMResponseMessageDelta(
                                response_id=response_id,
                                role="assistant",
                                content=full_content,
                                delta=full_content,
                                created=created,
                            )
                    else:
                        # Reasoning goes out under the reasoning types, which
                        # main_control renders but never speaks: _send_to_tts
                        # is gated on the event being a message. Without this
                        # the board's monologue is read aloud before the
                        # answer -- 235 characters of it, measured on an
                        # N1-655 on 2026-09-14.
                        splitter = _ReasoningSplitter()
                        reasoning = ""

                        async def _pairs():
                            async for chunk in self._iter_deltas(resp):
                                for pair in splitter.feed(chunk):
                                    yield pair
                            for pair in splitter.flush():
                                yield pair

                        async for kind, text in _pairs():
                            if kind == "reasoning":
                                reasoning += text
                                yield LLMResponseReasoningDelta(
                                    response_id=response_id,
                                    role="assistant",
                                    content=reasoning,
                                    delta=text,
                                    created=created,
                                )
                                yield LLMResponseReasoningDone(
                                    response_id=response_id,
                                    role="assistant",
                                    content=reasoning,
                                    created=created,
                                )
                                continue
                            if first_delta_at is None:
                                first_delta_at = time.monotonic()
                                self.ten_env.log_info(
                                    "[Ambarella] first answer token after "
                                    f"{first_delta_at - started:.1f}s "
                                    f"(reset={reset}, query_len={len(query)})",
                                    category=LOG_CATEGORY_KEY_POINT,
                                )
                            full_content += text
                            yield LLMResponseMessageDelta(
                                response_id=response_id,
                                role="assistant",
                                content=full_content,
                                delta=text,
                                created=created,
                            )
            except (asyncio.CancelledError, GeneratorExit):
                # main_control aborts the turn on barge-in. The board cannot
                # rewind the partial answer already appended to the session.
                if self.config.reset_after_abort:
                    self._needs_reset = True
                    # Reset-En clears the board-side history, which is where
                    # the system prompt lives once it has been folded into
                    # the first turn. Without this the session runs without
                    # a prompt from here on, and nothing says so.
                    self._prompt_sent = False
                self.ten_env.log_info(
                    "[Ambarella] turn aborted after "
                    f"{len(full_content)} chars"
                )
                raise

        elapsed = time.monotonic() - started
        ttft = (first_delta_at - started) if first_delta_at else elapsed
        self.ten_env.log_info(
            f"[Ambarella] turn done in {elapsed:.1f}s, "
            f"first token at {ttft:.1f}s, {len(full_content)} chars "
            f"(reset={reset})",
            category=LOG_CATEGORY_KEY_POINT,
        )
        yield LLMResponseMessageDone(
            response_id=response_id,
            role="assistant",
            content=full_content,
            created=created,
        )
