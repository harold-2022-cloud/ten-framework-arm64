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
        # The board serves one LLM user at a time, so at most one segment's
        # summary is ever in flight. This is which one the next answer
        # belongs to.
        self._awaiting_summary: Optional[str] = None

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

    async def on_deinit(self, _ten_env: AsyncTenEnv) -> None:
        self._stopped = True
        if self._ticker and not self._ticker.done():
            self._ticker.cancel()
        if self.agent:
            await self.agent.stop()

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

    async def _summarise_segment(self, segment_id: str) -> None:
        """One LLM turn per topic, as the topic lands.

        The board serves one user at a time and holds a session for 180 s after
        a reply, so these queue rather than overlap -- which the silences give
        room for.
        """
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
        self._awaiting_summary = segment_id
        await self.agent.queue_llm_input(self.config.segment_prompt + lines)

    def on_llm_text(self, text: str) -> None:
        """The board answers one turn at a time, so the answer belongs to the
        segment we last asked about. An answer with nothing waiting is dropped:
        the segment it belonged to has already been assembled, and filing it
        against whatever is current would put one topic's summary under
        another's heading.
        """
        if self._awaiting_summary is None:
            return
        self.record.add_summary(self._awaiting_summary, text.strip())
        self._awaiting_summary = None

    @agent_event_handler(LLMResponseEvent)
    async def _on_llm_response(self, event: LLMResponseEvent) -> None:
        if event.is_final and event.type == "message":
            self.on_llm_text(event.text)

    async def _assemble(self) -> None:
        if self.record.is_empty:
            self.ten_env.log_info("nothing was said; no record to assemble")
            return
        data = Data.create(DATA_MEETING_RECORD)
        data.set_property_from_json(
            None,
            json.dumps(
                {
                    "started_at": self.meeting_started_at,
                    "transcript": self.record.as_prompt_lines(
                        self.meeting_started_at
                    ),
                    "segments": [
                        {
                            "id": s.segment_id,
                            "summary": s.summary,
                            "error": s.error,
                        }
                        for s in self.record.ordered()
                    ],
                },
                ensure_ascii=False,
            ),
        )
        await self.ten_env.send_data(data)
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
