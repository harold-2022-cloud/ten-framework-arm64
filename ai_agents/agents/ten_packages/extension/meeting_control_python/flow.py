#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""One uploaded meeting, from file to archive.

Two rounds, in this order, because speaker numbers are only settled once
every topic is in:

  1. each topic is transcribed, one at a time, and written to record.json
     with the numbers its own diarization gave;
  2. every turn of the meeting is linked into meeting-wide numbers, the
     record is rewritten with them, and only then is each topic summarised
     -- a summary asked for earlier would quote numbers that are about to
     change -- followed by the meeting's conclusion.

Nothing here knows TEN. It talks through four ports -- segment_audio,
transcribe, ask_llm, log -- so the whole flow runs under test with the other
extensions played by a fake. Failure stays local: a topic that failed keeps
its place and its reason, a summary that never came is left empty, and only
an upload that cannot be decoded or holds no speech ends anywhere but
archived.
"""

import functools
import json
import os
import time
from typing import Callable, Optional

from .actions import parse_actions
from .config import MeetingControlConfig
from .minutes import render_minutes
from .script import convert_record, converter
from .record import MeetingRecord
from .speakers import link
from .state import write_state


# How often the words of a topic still being recognised are written to
# record.json: as often as the screen asks (every 2 s), not for every turn
# (one takes about 1.8 s on the board), since each write is the whole record.
LIVE_SAVE_S = 2.0


class MeetingFlow:
    def __init__(
        self, config: MeetingControlConfig, ports, clock=time.time
    ) -> None:
        self.config = config
        self.io = ports
        self.record: Optional[MeetingRecord] = None
        self.ogg_path = ""
        self.work_dir = ""
        self.folder = ""
        self.pcm_path = ""
        self.speakers = config.speakers
        self._convert: Callable[[str], str] = str  # set per meeting in start()
        self._busy_with: Optional[str] = None
        self._topics: list = []
        self._cursor = 0
        self._summary = ""
        self._actions: list = []
        self._actions_error: Optional[str] = None
        self._clock = clock
        # The topic in hand's words so far, and when they were last saved.
        self._live: list = []
        self._live_saved = 0.0

    # --- round 0: the upload -------------------------------------------

    async def start(self, payload: dict) -> None:
        meeting_id = payload["meeting_id"]
        if self._busy_with is not None:
            # The board's LLM serves one user at a time; a second meeting
            # would queue behind the first's summaries for an hour anyway.
            write_state(
                payload["work_dir"],
                "failed",
                error=f"meeting {self._busy_with} is still being processed",
            )
            self.io.log(
                f"turned {meeting_id} away: busy with {self._busy_with}"
            )
            return
        self._busy_with = meeting_id
        try:
            self.ogg_path = payload["ogg_path"]
            self.work_dir = payload["work_dir"]
            self.folder = os.path.dirname(self.ogg_path)
            self.speakers = payload.get("speakers") or self.config.speakers
            # Cached for the meeting: the record is written again and again
            # while its words come in, and its texts never change.
            self._convert = functools.lru_cache(maxsize=None)(
                self._script(payload.get("script"))
            )
            self.record = MeetingRecord(
                meeting_id,
                title=payload.get("title"),
                recorded_at=payload.get("recorded_at"),
                audio=os.path.basename(self.ogg_path),
            )
            self._topics, self._cursor = [], 0
            self._summary, self._actions, self._actions_error = "", [], None
            write_state(self.work_dir, "decoding", error=None)
        except Exception as err:  # pylint: disable=broad-except
            # Nothing has started and the meeting has no record yet: say
            # why on disk and be free for the next one. A package the board
            # lacked once left a meeting at "received" for good -- the
            # uploader refusing and pinging, the worker never reaped.
            self._busy_with = None
            self.io.log(f"{meeting_id} could not start: {err!r}")
            if payload.get("work_dir"):
                write_state(
                    payload["work_dir"],
                    "failed",
                    error=f"{type(err).__name__}: {err}",
                )
            return
        await self.io.segment_audio(self.ogg_path, self.work_dir)

    async def on_segments_ready(self, payload: dict) -> None:
        if self._busy_with is None:
            return
        self.record.duration_s = payload.get("duration_s") or 0.0
        if payload.get("error"):
            await self._end("failed", error=payload["error"])
            return
        if not payload["segments"]:
            await self._end("empty")
            return
        self.pcm_path = payload["pcm_path"]
        self._topics = payload["segments"]
        write_state(
            self.work_dir,
            "transcribing",
            topics_done=0,
            topics_total=len(self._topics),
        )
        await self._transcribe_next()

    # --- round 1: transcripts ------------------------------------------

    async def _transcribe_next(self) -> None:
        topic = self._topics[self._cursor]
        self._live, self._live_saved = [], 0.0
        # The transcriber starts by telling the topic's speakers apart: no
        # progress of its own until that is done, so the screen estimates
        # it from the topic's length.
        write_state(
            self.work_dir,
            "transcribing",
            step="diarizing",
            step_started_at=self._clock(),
            topic_s=topic["end_s"] - topic["start_s"],
            turns_done=0,
            turns_total=0,
        )
        await self.io.transcribe(
            self.pcm_path,
            topic["id"],
            topic["start_s"],
            topic["end_s"] - topic["start_s"],
            self.speakers,
        )

    async def on_progress(self, payload: dict) -> None:
        """How far the transcriber got with the topic in hand: the turns
        recognised and their words, for the screen. For show only: a report
        for another topic, a late one or a broken one is dropped, never
        raised -- it must not end the meeting or turn its state back."""
        try:
            self._progress(payload)
        except Exception as err:  # pylint: disable=broad-except
            self.io.log(f"progress dropped: {type(err).__name__}: {err}")

    async def on_progress_json(self, text: str) -> None:
        """on_progress for the report as it came, parsed here: the
        extension hands it on as text, outside the guard that would end the
        meeting over a payload that does not parse."""
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as err:
            self.io.log(f"progress dropped: {type(err).__name__}: {err}")
            return
        await self.on_progress(payload)

    def _progress(self, payload: dict) -> None:
        if self._busy_with is None or self._cursor >= len(self._topics):
            return
        topic = self._topics[self._cursor]
        if payload.get("segment_id") != topic["id"]:
            return
        done = int(payload.get("turns_done") or 0)
        total = int(payload.get("turns_total") or 0)
        fields = {"step": "recognising", "turns_done": done}
        fields["turns_total"] = total
        if done == 0:
            fields["step_started_at"] = self._clock()
        utterance = payload.get("utterance")
        if isinstance(utterance, dict):
            self._live.append(dict(utterance))
            now = self._clock()
            if now - self._live_saved >= LIVE_SAVE_S or done >= total:
                # Replaced by the finished topic in on_transcribed.
                self.record.add_topic(
                    topic["id"], topic["start_s"], topic["end_s"], self._live
                )
                self._persist()
                self._live_saved = now
        write_state(self.work_dir, "transcribing", **fields)

    async def on_transcribed(self, payload: dict) -> None:
        if self._busy_with is None or self._cursor >= len(self._topics):
            return
        topic = self._topics[self._cursor]
        if payload.get("segment_id") != topic["id"]:
            self.io.log(
                f"ignoring a transcript for {payload.get('segment_id')}"
            )
            return
        if payload.get("error"):
            self.record.mark_failed(
                topic["id"], topic["start_s"], topic["end_s"], payload["error"]
            )
        else:
            timing = payload.get("timing")
            self.record.add_topic(
                topic["id"],
                topic["start_s"],
                topic["end_s"],
                payload.get("utterances") or [],
                timing=timing,
            )
            if timing:
                self.io.log(
                    f"topic {topic['id']} "
                    f"({topic['end_s'] - topic['start_s']:.0f} s): "
                    f"diarized {timing.get('diarize_s')} s, "
                    f"recognised {timing.get('recognise_s')} s, "
                    f"embedded {timing.get('embed_s')} s, "
                    f"{timing.get('turns')} turns"
                )
        # On disk before anything slow happens next.
        self._persist()
        self._cursor += 1
        write_state(self.work_dir, "transcribing", topics_done=self._cursor)
        if self._cursor < len(self._topics):
            await self._transcribe_next()
            return
        try:
            await self._finish()
        except Exception as err:  # pylint: disable=broad-except
            await self._end("failed", error=f"{type(err).__name__}: {err}")

    # --- round 2: speakers, summaries, conclusion ----------------------

    async def _finish(self) -> None:
        write_state(self.work_dir, "linking")
        labels = link(
            self.record.turns(),
            self.speakers,
            over_factor=self.config.link_over_factor,
            threshold=self.config.link_threshold,
            same_voice=self.config.link_same_voice,
        )
        self.record.relabel(labels)
        self._persist()

        topics = self.record.ordered()
        write_state(
            self.work_dir,
            "summarising",
            topics_done=0,
            topics_total=len(topics),
        )
        for done, topic in enumerate(topics, 1):
            if not topic.error and topic.utterances:
                answer = await self._ask(
                    self.config.segment_prompt
                    + self.record.llm_lines_for(topic.id)
                )
                self.record.add_summary(topic.id, answer)
                self._persist()
            write_state(self.work_dir, "summarising", topics_done=done)

        write_state(self.work_dir, "concluding")
        # Numbered, not timed: a clock in front of the 7B invites copying.
        summaries = "\n\n".join(
            f"第{n}段：{t.summary}"
            for n, t in enumerate(topics, 1)
            if t.summary
        )
        if summaries:
            self._summary = await self._ask(
                self.config.meeting_prompt + summaries
            )
        self._actions, self._actions_error = parse_actions(self._summary)
        await self._end("archived")

    async def _ask(self, prompt: str) -> str:
        """The LLM's answer, or "" -- a turn that fails costs its summary,
        never the transcript or the rest of the meeting."""
        try:
            return (await self.io.ask_llm(prompt) or "").strip()
        except Exception as err:  # pylint: disable=broad-except
            self.io.log(f"LLM turn failed: {type(err).__name__}: {err}")
            return ""

    # --- the end, whichever it is ---------------------------------------

    async def abandon(self, error: str) -> None:
        """Something raised past the flow's own handling: the meeting in hand
        ends failed with why, so the uploader stops refusing and pinging and
        the next meeting can come."""
        if self._busy_with is None:
            return
        try:
            await self._end("failed", error=error)
        except Exception as err:  # pylint: disable=broad-except
            self._busy_with = None
            self.io.log(f"could not end the meeting cleanly: {err!r}")
            write_state(self.work_dir, "failed", error=error)

    async def _end(self, state: str, error: Optional[str] = None) -> None:
        self._persist(error=error)
        self._write(
            "minutes.txt",
            self._convert(
                render_minutes(self.record, self._summary, self._actions)
            ),
        )
        if state == "archived":
            pcm = os.path.join(self.work_dir, "audio.pcm")
            if os.path.exists(pcm):
                os.remove(pcm)
        write_state(self.work_dir, state, error=error)
        self.io.log(f"meeting {self._busy_with} ended {state}")
        self._busy_with = None

    def _persist(self, error: Optional[str] = None) -> None:
        payload = convert_record(
            self.record.to_json(
                summary=self._summary,
                actions=self._actions,
                actions_error=self._actions_error,
                error=error,
            ),
            self._convert,
        )
        self._write(
            "record.json", json.dumps(payload, ensure_ascii=False, indent=2)
        )

    def _script(self, chosen: Optional[str]) -> Callable[[str], str]:
        """The upload's choice of script, else the board's default, else
        Simplified. The uploader refuses an unknown choice and the default
        comes from the graph's environment; either can still be wrong by
        the time it gets here, and that is logged, never fatal: a typo in
        .env must not leave every meeting stuck."""
        for script in (chosen, self.config.output_script):
            if not script:
                continue
            try:
                return converter(script)
            except ValueError as err:
                self.io.log(f"{err}; ignored")
        return converter("simplified")

    def _write(self, name: str, text: str) -> None:
        path = os.path.join(self.folder, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
