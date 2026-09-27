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
from typing import Callable, Optional

from ten_runtime import (
    AsyncExtension,
    AsyncTenEnv,
    AudioFrame,
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
        # Somebody is talking right now: True between start_of_sentence and
        # end_of_sentence. The VAD emits nothing in between, so without this
        # the ticker reads "time since the last VAD event" as elapsed
        # silence and cuts a long monologue in half.
        self._speaking = False
        # When a pcm_frame was last seen. The only evidence the upload is
        # still there, and the only clock that keeps running after a drop.
        self._last_frame_at = 0.0
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
        # Computed once, so every write of one meeting lands on one file.
        self._record_file: Optional[str] = None
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
        # Kept rather than passed, so that a write triggered by a segment
        # landing after an early assembly does not blank out the
        # conclusions that assembly already produced.
        self._meeting_summary: str = ""

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
        now = time.time()
        if self.meeting_started_at == 0.0:
            self.meeting_started_at = now
        self._speaking = True
        self._last_speech_at = now
        self._segment_pending = True
        # A meeting can resume after the long threshold already assembled
        # it once; talking again means a later lull must be able to
        # assemble it again, not be silently absorbed forever.
        self._assembled = False
        if self._last_frame_at == 0.0:
            # The VAD only ever speaks because frames reached it. A graph
            # that does not route pcm_frame here as well leaves this as the
            # only evidence of an upload there will ever be, and without any
            # such evidence a drop mid-sentence would wait for ever.
            self._last_frame_at = now

    def speech_stopped(self) -> None:
        """A silence begins here. The ticker decides what it means."""
        self._speaking = False
        self._last_speech_at = time.time()

    def _quiet_for(self) -> Optional[float]:
        """Elapsed silence, or ``None`` while somebody is still talking.

        Two different questions, answered from two different clocks.

        "Has this topic ended?" is about speech, and speech is over only
        when the VAD says so. ``ten_vad_python`` is a state machine: it
        emits ``start_of_sentence`` once on IDLE->SPEAKING,
        ``end_of_sentence`` once on SPEAKING->IDLE, and nothing at all in
        between. Time since the last VAD *event* is therefore not time
        since speech stopped, and reading it as such cut any monologue
        longer than ``segment_silence_s`` in half, mid-word, and stranded
        the rest of it in a segment nothing ever closed.

        "Is the upload still there?" cannot be asked of the VAD at all:
        ``websocket_server`` only logs a client disconnect and emits
        nothing into the graph, so a drop mid-sentence produces no
        ``end_of_sentence`` ever, and a clock gated purely on VAD state
        would hang there for good. The frames answer it instead -- they
        stop arriving whether or not the VAD had its say. Past
        ``upload_gone_s`` of no frames the speech is over whatever the VAD
        last said, and it ended when the audio did.
        """
        if self._last_speech_at == 0.0:
            return None
        now = time.time()
        if self._speaking:
            if (
                self._last_frame_at == 0.0
                or now - self._last_frame_at < self.config.upload_gone_s
            ):
                return None
            self._speaking = False
            self._last_speech_at = self._last_frame_at
        return now - self._last_speech_at

    async def on_audio_frame(
        self, _ten_env: AsyncTenEnv, _audio_frame: AudioFrame
    ) -> None:
        """A timestamp, and nothing else.

        The recorder owns the audio. What main_control needs from the
        stream is the one thing the VAD cannot tell it: whether the upload
        is still there.
        """
        self._last_frame_at = time.time()

    async def _tick(self) -> None:
        """One ticker, not two timers armed when speech ends.

        A network drop mid-sentence produces no end_of_sentence at all, so
        timers armed on that event would never be armed and the record
        would never be assembled. Polling ``_quiet_for()`` answers the same
        questions and answers them after a disconnect too.
        """
        while not self._stopped:
            await asyncio.sleep(self._tick_s)
            quiet_for = self._quiet_for()
            if quiet_for is None:
                continue
            if (
                self._segment_pending
                and quiet_for >= self.config.segment_silence_s
            ):
                self._segment_pending = False
                await self._close_segment()
            # Re-derived, not reused: the await above can take long enough
            # for speech to resume, and a quiet_for computed before that
            # await would assemble on silence that has already ended.
            quiet_for = self._quiet_for()
            if quiet_for is None:
                continue
            if (
                not self._assembled
                and quiet_for >= self.config.meeting_silence_s
            ):
                self._assembled = True
                await self._assemble()

    # --- driving the workers ---------------------------------------------

    async def _close_segment(self) -> None:
        await self.ten_env.send_cmd(Cmd.create(CMD_CLOSE_SEGMENT))

    async def _ask_llm(self, key: str, build_prompt: Callable[[], str]) -> str:
        """One LLM turn, asked and waited for.

        ``build_prompt`` is called only once this turn actually holds
        ``_summary_lock`` -- not before. A prompt built by the caller ahead
        of the lock would still see the state of things at call time; if
        this turn had to wait behind another one, whatever that other turn
        changed (typically: a segment's own summary landing in the record)
        would already be true by the time this turn is finally asked, but
        the *string* would not reflect it. Composing it here is what makes
        the two agree. An empty result means there is nothing worth asking
        yet, and this returns "" without ever touching the slot.

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
            prompt = build_prompt()
            if not prompt:
                return ""
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

        def build_prompt() -> str:
            # The meeting's clock, not the segment file's. These summaries
            # are the only input the meeting-level turn ever sees, so a
            # topic asked about on its own local clock can only answer on
            # that clock: every topic's 待辦 would begin again from 00:00,
            # two different action items from two different topics would
            # both read [01:15], and neither would point at anything a
            # person can find in the meeting or seek to in the recording.
            lines = self.record.lines_for(segment_id, self.meeting_started_at)
            if not lines:
                return ""
            return self.config.segment_prompt + lines

        answer = await self._ask_llm(segment_id, build_prompt)
        if answer:
            self.record.add_summary(segment_id, answer)
            self._persist_record()

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

    def _record_path(self) -> Optional[str]:
        """One file per meeting, named after the meeting it holds.

        ``meeting_recorder`` deliberately keeps one directory per
        deployment because the file names carry the time -- but only the
        *audio*'s did. A fixed ``meeting_record.json`` meant the next
        meeting destroyed the previous one's minutes, and so did a single
        meeting that assembled twice (talking again after a long lull
        re-arms assembly -- the deliberate 「最多是記錄早產出一次」
        behaviour, which should cost nothing). Keyed on the meeting's own
        start, so every write within one meeting lands on the same file.
        """
        if not self._last_segment_dir:
            return None
        if self._record_file is None:
            # Latched, not recomputed. The fallback below is unreachable on
            # every path that exists today -- speech_started() sets
            # meeting_started_at before anything can persist -- but a bare
            # `or time.time()` evaluated per call would hand each write its
            # own filename, which is the very thing one file per meeting
            # exists to prevent.
            started = self.meeting_started_at or time.time()
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(started))
            self._record_file = os.path.join(
                self._last_segment_dir, f"meeting_record_{stamp}.json"
            )
        return self._record_file

    def _write_record_to_disk(self, record_payload: dict) -> None:
        """The graph gives ``meeting_record`` nowhere to route to -- no node
        declares it as ``data_in`` -- so a file next to the audio it
        describes is the record's only real destination. The most recently
        seen ``segment_closed`` told us that directory.
        """
        record_path = self._record_path()
        if not record_path:
            self.ten_env.log_error(
                "no segment directory seen yet; meeting record not "
                "written to disk"
            )
            return
        try:
            with open(record_path, "w", encoding="utf-8") as f:
                json.dump(record_payload, f, ensure_ascii=False, indent=2)
            self.ten_env.log_info(f"meeting record written to {record_path}")
        except OSError as exc:
            self.ten_env.log_error(
                f"failed to write meeting record to {record_path}: {exc}"
            )

    def _record_note(self) -> str:
        """What to say when the record cannot speak for itself.

        The spec's 出錯的時候 table says the transcript is 「永遠完整附上」
        and that a failed segment leaves 「記錄裡註明第 N 段未能處理，原始
        錄音檔留著」. When the failure is systematic -- a missing model
        path, an OOM on this board -- every segment fails the same way, and
        suppressing the record then hides the one case where that note is
        worth the most.
        """
        if not self.record.is_empty:
            return ""
        return (
            "所有段落都未能處理，沒有任何逐字稿。原始錄音檔留在 "
            f"{self._last_segment_dir or '(未知)'}。"
        )

    def _record_payload(self) -> dict:
        """The record as it stands, ready to be written or sent.

        ``header`` is the shape 產出的形狀 opens with -- the meeting's time
        as a range with a duration, and how many people were in it -- and
        the three fields it is rendered from are carried alongside it so
        anything reading this file does not have to parse a sentence.
        """
        ended_at = self.record.ended_at or self.meeting_started_at
        return {
            "header": self.record.header(self.meeting_started_at),
            "started_at": self.meeting_started_at,
            "ended_at": ended_at,
            "duration_s": max(0.0, ended_at - self.meeting_started_at),
            "speaker_count": self.record.speaker_count,
            "note": self._record_note(),
            "transcript": self.record.as_prompt_lines(self.meeting_started_at),
            "meeting_summary": self._meeting_summary,
            "segments": [
                {
                    "id": s.segment_id,
                    "summary": s.summary,
                    "error": s.error,
                }
                for s in self.record.ordered()
            ],
        }

    def _persist_record(self) -> None:
        """Everything processed so far, on disk, now.

        The spec counts 「中途掛掉不會全失。已經處理完的段落留在磁碟上」 as
        one of the four things cutting a meeting into topics buys. Until
        assembly the only thing on disk was the undecoded ``.pcm``: the
        diarization, the transcription and every per-topic summary -- all
        of the expensive work -- lived in memory and died with the worker.

        That is not a remote possibility. The runtime calls ``os._exit(1)``
        on an uncaught exception in any extension callback, and
        ``SegmentWriter.write`` is unguarded, so an ENOSPC on one frame of
        a long meeting does not degrade anything: it terminates the process
        and the meeting's record with it.
        """
        try:
            payload = self._record_payload()
        except Exception as exc:  # pylint: disable=broad-except
            # Building the payload reads the shape of every message this
            # extension has been handed. _write_record_to_disk guards the
            # write; nothing guarded the construction, and the runtime
            # calls os._exit(1) on an uncaught exception in a callback.
            # Losing one intermediate save beats killing the meeting on
            # the path whose whole purpose is not losing the meeting.
            self.ten_env.log_error(f"could not build the record: {exc}")
            return
        self._write_record_to_disk(payload)

    async def _assemble(self) -> None:
        # Not `is_empty`: a meeting in which every segment failed has
        # nothing said in it and everything to explain. Suppressing the
        # record there left the user with no file and no reason -- exactly
        # the case the spec's 「記錄裡註明第 N 段未能處理」 is for, and the
        # one where a systematic failure (a missing model path, an OOM on
        # this board) makes every segment fail the same way. Only a record
        # that never heard of a segment at all has nothing to say.
        if not self.record.ordered():
            self.ten_env.log_info("nothing was said; no record to assemble")
            return

        def build_meeting_prompt() -> str:
            # Read fresh, inside _ask_llm's critical section: if a
            # segment's own turn is still outstanding when the
            # meeting-ending silence fires, this call waits behind it on
            # the same lock, and by the time it runs that segment's
            # summary is already in the record. Reading self.record here
            # rather than closing over a string built before the wait is
            # what makes the two agree -- otherwise the meeting's
            # conclusions could silently omit exactly the segment that
            # contained them.
            summaries = "\n\n".join(
                s.summary for s in self.record.ordered() if s.summary
            )
            return self.config.meeting_prompt + summaries if summaries else ""

        # The input is the per-segment summaries, not the full transcript,
        # so this turn stays short; the transcript itself always goes out
        # regardless of whether this turn succeeds -- or of whether there
        # was anything to ask about at all, which build_meeting_prompt
        # alone decides, for the same freshness reason.
        self._meeting_summary = await self._ask_llm(
            MEETING_SUMMARY_KEY, build_meeting_prompt
        )
        record_payload = self._record_payload()
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
                self._persist_record()
                return
            self.record.add_segment(
                segment_id, started_at, payload["utterances"]
            )
            # On disk before the LLM is asked anything, not after: a turn
            # that hangs for summary_timeout_s must not be what stands
            # between this topic's transcript and the file.
            self._persist_record()
            await self._summarise_segment(segment_id)
