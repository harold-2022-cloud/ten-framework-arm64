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

    def as_prompt_lines(self, started_at: float) -> str:
        """Both clocks on every line: the wall clock for a person, the offset
        for finding the moment in the recording."""
        out: List[str] = []
        for segment in self.ordered():
            if segment.error:
                out.append(
                    f"（第 {segment.segment_id} 段未能處理：{segment.error}）"
                )
                continue
            for utterance in segment.utterances:
                absolute = segment.started_at + utterance["start_s"]
                offset = absolute - started_at
                clock = time.strftime("%H:%M", time.localtime(absolute))
                out.append(
                    f"[{clock} / {int(offset // 60):02d}:{int(offset % 60):02d}] "
                    f"說話人{utterance['speaker']}: {utterance['text']}"
                )
        return "\n".join(out)
