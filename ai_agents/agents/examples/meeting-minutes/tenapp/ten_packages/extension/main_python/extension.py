#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Times the silences, drives the two workers, owns the record.

Silence is a topic boundary, not the end of the meeting. Two timers run from
the same moment: the short one closes a topic and the long one decides nobody
is coming back. Getting the long one wrong costs a record assembled early,
because the topics are already done by then.
"""

import asyncio
import json
import os
import time
import uuid
from typing import Optional

from ten_runtime import (
    AsyncExtension,
    AsyncTenEnv,
    Cmd,
    CmdResult,
    Data,
    StatusCode,
)

from .agent.agent import Agent
from .agent.decorators import agent_event_handler
from .agent.events import LLMResponseEvent
from .config import MeetingControlConfig
from .record import MeetingRecord

CMD_CLOSE_SEGMENT = "close_segment"
CMD_TRANSCRIBE = "transcribe"
DATA_SEGMENT_CLOSED = "segment_closed"
DATA_SEGMENT_TRANSCRIBED = "segment_transcribed"
DATA_MEETING_RECORD = "meeting_record"
# Not a real segment: the key _ask_llm/on_llm_text use for the one turn that
# asks about the whole meeting rather than a single topic.
MEETING_SUMMARY_KEY = "__meeting__"


class MeetingControlExtension(AsyncExtension):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.ten_env: Optional[AsyncTenEnv] = None
        self.config: Optional[MeetingControlConfig] = None
        self.agent: Optional[Agent] = None
        self.record = MeetingRecord()
        self.meeting_started_at = 0.0
        self._last_speech_at = 0.0
        self._segment_pending = False
        self._assembled = False
        self._stopped = False
        self._tick_s = 1.0
        self._ticker: Optional[asyncio.Task] = None
        self._pending: dict = {}
        # The most recently seen segment's own directory: the graph gives
        # meeting_record nowhere to route to, so a file next to that audio
        # is the record's only real destination.
        self._last_segment_dir: Optional[str] = None
        # The board serves one LLM user at a time, so at most one turn -- a
        # segment's summary or the whole meeting's -- is ever in flight.
        # This lock serialises set-slot -> queue -> wait-for-answer ->
        # file-answer, so a second topic transcribed while the first is
        # still being summarised cannot steal its answer: queuing a prompt
        # never blocks, only actually holding this lock does.
        self._summary_lock = asyncio.Lock()
        self._awaiting_summary: Optional[str] = None
        self._summary_done: Optional[asyncio.Event] = None
        self._last_answer: str = ""

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        self.ten_env = ten_env
        config_json, _ = await ten_env.get_property_to_json("")
        self.config = MeetingControlConfig.model_validate_json(config_json)
        self.agent = Agent(ten_env)
        for attr_name in dir(self):
            fn = getattr(self, attr_name)
            event_type = getattr(fn, "_agent_event_type", None)
            if event_type:
                self.agent.on(event_type, fn)
        self._ticker = asyncio.create_task(self._tick())

    async def on_stop(self, _ten_env: AsyncTenEnv) -> None:
        # A live, cross-extension operation (LLMExec.stop() can send an
        # "abort" cmd to "llm"), so it belongs here, not in on_deinit: the
        # graph is still up during on_stop, and CLAUDE.md says clean up
        # here for exactly this reason.
        if self.agent:
            await self.agent.stop()

    async def on_deinit(self, _ten_env: AsyncTenEnv) -> None:
        self._stopped = True
        if self._ticker and not self._ticker.done():
            self._ticker.cancel()

    # --- the clock --------------------------------------------------------

    def speech_started(self) -> None:
        """Somebody is talking. Nothing has gone quiet."""
        if self.meeting_started_at == 0.0:
            self.meeting_started_at = time.time()
        self._last_speech_at = time.time()
        self._segment_pending = True
        # A meeting can resume after the long threshold already assembled
        # it once; talking again means a later lull must be able to
        # assemble it again, not be silently absorbed forever.
        self._assembled = False

    def speech_stopped(self) -> None:
        """A silence begins here. The ticker decides what it means."""
        self._last_speech_at = time.time()

    async def _tick(self) -> None:
        """One ticker, not two timers armed when speech ends.

        A network drop mid-sentence produces no end_of_sentence at all, so
        timers armed on that event would never be armed and the record would
        never be assembled. Elapsed time since the last speech answers the
        same questions and answers them after a disconnect too.
        """
        while not self._stopped:
            await asyncio.sleep(self._tick_s)
            if self._last_speech_at == 0.0:
                continue
            quiet_for = time.time() - self._last_speech_at
            if (
                self._segment_pending
                and quiet_for >= self.config.segment_silence_s
            ):
                self._segment_pending = False
                await self._close_segment()
            # Re-derived, not reused: the await above can take long enough
            # for speech_started() to move _last_speech_at, and a quiet_for
            # computed before that await would assemble on silence that has
            # already ended.
            quiet_for = time.time() - self._last_speech_at
            if (
                not self._assembled
                and quiet_for >= self.config.meeting_silence_s
            ):
                self._assembled = True
                await self._assemble()

    # --- driving the workers ---------------------------------------------

    async def _close_segment(self) -> None:
        await self.ten_env.send_cmd(Cmd.create(CMD_CLOSE_SEGMENT))

    async def _ask_llm(self, key: str, prompt: str) -> str:
        """One LLM turn, asked and waited for.

        Queuing a prompt never blocks -- ``AsyncQueue.put`` always returns --
        so setting ``_awaiting_summary`` at queue time and moving on let a
        second topic (or the whole-meeting turn) overwrite the slot while
        the first turn was still in flight, and its answer landed under the
        wrong heading. Holding ``_summary_lock`` across set-slot -> queue ->
        wait-for-answer -> file-answer is what actually serialises turns,
        matching the board itself, which only ever holds one.

        A hung board must not stop every later topic from being summarised,
        so the wait is bounded by ``summary_timeout_s``: past that, this
        gives up, frees the slot, and returns "" -- the caller still emits
        whatever it has (the spec: a failed summary must not cost the
        transcript).
        """
        async with self._summary_lock:
            self._awaiting_summary = key
            self._summary_done = asyncio.Event()
            try:
                await self.agent.queue_llm_input(prompt)
                await asyncio.wait_for(
                    self._summary_done.wait(),
                    timeout=self.config.summary_timeout_s,
                )
            except asyncio.TimeoutError:
                self.ten_env.log_error(
                    f"LLM turn for {key!r} timed out after "
                    f"{self.config.summary_timeout_s}s"
                )
                # The answer carries no correlation id, so nothing in
                # on_llm_text can tell a late reply to this turn apart from
                # whatever turn asks next -- the only sound fix is to make
                # a late answer not exist. This aborts the board's turn and
                # cancels the task reading its response, so a stray final
                # cannot arrive once this turn has already given up.
                await self.agent.flush_llm()
            except Exception as exc:  # pylint: disable=broad-except
                self.ten_env.log_error(f"LLM turn for {key!r} failed: {exc}")
            finally:
                self._awaiting_summary = None
                self._summary_done = None
            answer = self._last_answer
            self._last_answer = ""
            return answer

    async def _summarise_segment(self, segment_id: str) -> None:
        """One LLM turn per topic, as the topic lands."""
        entry = next(
            (s for s in self.record.ordered() if s.segment_id == segment_id),
            None,
        )
        if entry is None or not entry.utterances:
            return
        lines = "\n".join(
            f"[{int(u['start_s'] // 60):02d}:{int(u['start_s'] % 60):02d}] "
            f"說話人{u['speaker']}: {u['text']}"
            for u in entry.utterances
        )
        answer = await self._ask_llm(
            segment_id, self.config.segment_prompt + lines
        )
        if answer:
            self.record.add_summary(segment_id, answer)

    def on_llm_text(self, text: str) -> None:
        """The board answers one turn at a time, so this answer belongs to
        whichever call to ``_ask_llm`` is still waiting. An answer with
        nothing waiting is dropped: either it already timed out and moved
        on, or the segment it belonged to has already been assembled, and
        filing it against whatever is current would put one topic's summary
        under another's heading.
        """
        if self._awaiting_summary is None:
            return
        self._last_answer = text.strip()
        if self._summary_done is not None:
            self._summary_done.set()

    @agent_event_handler(LLMResponseEvent)
    async def _on_llm_response(self, event: LLMResponseEvent) -> None:
        if event.is_final and event.type == "message":
            self.on_llm_text(event.text)

    def _write_record_to_disk(self, record_payload: dict) -> None:
        """The graph gives ``meeting_record`` nowhere to route to -- no node
        declares it as ``data_in`` -- so a file next to the audio it
        describes is the record's only real destination. The most recently
        seen ``segment_closed`` told us that directory.
        """
        if not self._last_segment_dir:
            self.ten_env.log_error(
                "no segment directory seen yet; meeting record not "
                "written to disk"
            )
            return
        record_path = os.path.join(
            self._last_segment_dir, "meeting_record.json"
        )
        try:
            with open(record_path, "w", encoding="utf-8") as f:
                json.dump(record_payload, f, ensure_ascii=False, indent=2)
            self.ten_env.log_info(f"meeting record written to {record_path}")
        except OSError as exc:
            self.ten_env.log_error(
                f"failed to write meeting record to {record_path}: {exc}"
            )

    async def _assemble(self) -> None:
        if self.record.is_empty:
            self.ten_env.log_info("nothing was said; no record to assemble")
            return
        segments = self.record.ordered()
        # The input is the per-segment summaries, not the full transcript,
        # so this turn stays short; the transcript itself always goes out
        # regardless of whether this turn succeeds.
        summaries = "\n\n".join(s.summary for s in segments if s.summary)
        meeting_summary = ""
        if summaries:
            meeting_summary = await self._ask_llm(
                MEETING_SUMMARY_KEY, self.config.meeting_prompt + summaries
            )
        record_payload = {
            "started_at": self.meeting_started_at,
            "transcript": self.record.as_prompt_lines(self.meeting_started_at),
            "meeting_summary": meeting_summary,
            "segments": [
                {
                    "id": s.segment_id,
                    "summary": s.summary,
                    "error": s.error,
                }
                for s in segments
            ],
        }
        data = Data.create(DATA_MEETING_RECORD)
        data.set_property_from_json(
            None, json.dumps(record_payload, ensure_ascii=False)
        )
        await self.ten_env.send_data(data)
        self._write_record_to_disk(record_payload)
        self.ten_env.log_info("meeting record assembled")

    async def on_cmd(self, ten_env: AsyncTenEnv, cmd: Cmd) -> None:
        name = cmd.get_name()
        if name == "start_of_sentence":
            self.speech_started()
        elif name == "end_of_sentence":
            self.speech_stopped()
        await ten_env.return_result(CmdResult.create(StatusCode.OK, cmd))

    async def on_data(self, ten_env: AsyncTenEnv, data: Data) -> None:
        name = data.get_name()
        payload_json, _ = data.get_property_to_json(None)
        payload = json.loads(payload_json)

        if name == DATA_SEGMENT_CLOSED:
            # Tracked regardless of the min_segment_s filter below: even a
            # too-short segment lives in the same output_dir, and it may be
            # the last one seen before the meeting ends.
            self._last_segment_dir = os.path.dirname(payload["path"])
            if payload["duration_s"] < self.config.min_segment_s:
                # Too short to be a topic. Its audio joins the next segment,
                # which is what happens by itself: the recorder opens a new
                # file on the next frame and this one is simply not sent on.
                ten_env.log_info(
                    f"segment of {payload['duration_s']:.1f} s ignored, "
                    f"under {self.config.min_segment_s} s"
                )
                return
            segment_id = uuid.uuid4().hex[:8]
            self._pending[segment_id] = payload["started_at"]
            cmd = Cmd.create(CMD_TRANSCRIBE)
            cmd.set_property_string("path", payload["path"])
            cmd.set_property_string("segment_id", segment_id)
            cmd.set_property_int("speakers", self.config.speakers)
            await ten_env.send_cmd(cmd)

        elif name == DATA_SEGMENT_TRANSCRIBED:
            segment_id = payload["segment_id"]
            started_at = self._pending.pop(segment_id, 0.0)
            if payload.get("error"):
                self.record.mark_failed(
                    segment_id, payload["error"], started_at
                )
                return
            self.record.add_segment(
                segment_id, started_at, payload["utterances"]
            )
            await self._summarise_segment(segment_id)
