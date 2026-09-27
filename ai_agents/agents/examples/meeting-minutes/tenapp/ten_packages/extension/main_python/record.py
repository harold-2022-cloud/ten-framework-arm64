#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The meeting record, as it grows.

Segments arrive out of order -- one can be summarising while the next is being
recorded -- so the record sorts rather than appends, and a segment that failed
keeps its place with the reason in it.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SegmentRecord:
    segment_id: str
    started_at: float
    utterances: List[dict] = field(default_factory=list)
    summary: str = ""
    error: Optional[str] = None


class MeetingRecord:
    def __init__(self) -> None:
        self._segments: Dict[str, SegmentRecord] = {}

    @property
    def is_empty(self) -> bool:
        return not any(s.utterances for s in self._segments.values())

    def add_segment(self, segment_id, started_at, utterances) -> None:
        entry = self._segments.setdefault(
            segment_id, SegmentRecord(segment_id, started_at)
        )
        entry.started_at = started_at
        entry.utterances = list(utterances)

    def add_summary(self, segment_id: str, text: str) -> None:
        if segment_id in self._segments:
            self._segments[segment_id].summary = text

    def mark_failed(
        self, segment_id: str, error: str, started_at: float = 0.0
    ) -> None:
        entry = self._segments.setdefault(
            segment_id, SegmentRecord(segment_id, started_at)
        )
        entry.started_at = started_at
        entry.error = error

    def ordered(self) -> List[SegmentRecord]:
        return sorted(self._segments.values(), key=lambda s: s.started_at)

    @property
    def ended_at(self) -> float:
        """Wall-clock epoch of the last word said, or 0.0 if none was.

        Taken from the utterances rather than from when assembly ran:
        assembly happens meeting_silence_s -- ten minutes by default --
        after the meeting actually ended, and a 會議時間 range stretched to
        cover that silence would be wrong by exactly that much.
        """
        latest = 0.0
        for segment in self._segments.values():
            for utterance in segment.utterances:
                latest = max(latest, segment.started_at + utterance["end_s"])
        return latest

    @property
    def speaker_count(self) -> int:
        """與會 N 人, counted from what the diarizer actually produced.

        It is *told* how many speakers to expect, but what reached the
        record is what the record can honestly claim -- a meeting where one
        of the three never spoke had two people in it, as far as anything
        here can know.
        """
        return len(
            {
                utterance["speaker"]
                for segment in self._segments.values()
                for utterance in segment.utterances
            }
        )

    def header(self, started_at: float) -> str:
        """The two lines 產出的形狀 opens with, before 結論 and 待辦."""
        ended_at = self.ended_at or started_at
        minutes = int(max(0.0, ended_at - started_at) // 60)
        day = time.strftime("%Y-%m-%d", time.localtime(started_at))
        opened = time.strftime("%H:%M", time.localtime(started_at))
        closed = time.strftime("%H:%M", time.localtime(ended_at))
        return (
            f"會議時間 {day} {opened} – {closed}（{minutes} 分鐘）\n"
            f"與會 {self.speaker_count} 人"
        )

    @staticmethod
    def _segment_lines(segment: SegmentRecord, started_at: float) -> List[str]:
        """Both clocks on every line: the wall clock for a person, the offset
        for finding the moment in the recording.

        ``started_at`` is the meeting's start, not the segment's. A
        segment's own ``start_s`` values are offsets into its own file, so
        a line rendered from them alone restarts at 00:00 on every topic
        and points at nothing anybody can find.
        """
        if segment.error:
            return [f"（第 {segment.segment_id} 段未能處理：{segment.error}）"]
        out: List[str] = []
        for utterance in segment.utterances:
            absolute = segment.started_at + utterance["start_s"]
            offset = absolute - started_at
            clock = time.strftime("%H:%M", time.localtime(absolute))
            out.append(
                f"[{clock} / {int(offset // 60):02d}:{int(offset % 60):02d}] "
                f"說話人{utterance['speaker']}: {utterance['text']}"
            )
        return out

    def lines_for(self, segment_id: str, started_at: float) -> str:
        """One topic's lines, on the same two clocks the transcript uses.

        The per-topic summary is the only input the meeting-level turn ever
        gets, so a topic asked about on its own local clock can only answer
        on that clock, and every topic's 待辦 then begins again from zero.
        """
        segment = self._segments.get(segment_id)
        if segment is None:
            return ""
        return "\n".join(self._segment_lines(segment, started_at))

    def as_prompt_lines(self, started_at: float) -> str:
        out: List[str] = []
        for segment in self.ordered():
            out.extend(self._segment_lines(segment, started_at))
        return "\n".join(out)
