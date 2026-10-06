#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The meeting record, as it grows topic by topic.

Every time here is a position in the uploaded file. A topic knows where it
starts; an utterance knows where it starts inside its topic; the two add up
to the moment a person can seek to in audio.ogg. The wall clock appears
only when the uploader said when recording began, and only on the page --
it decides nothing.

Adapted from the streaming version (67085cfef), which added a segment's
wall-clock start to an utterance's offset; there are no wall clocks left to
add, only two offsets into one file.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .speakers import Turn


@dataclass
class TopicRecord:
    id: str
    start_s: float
    end_s: float
    utterances: List[dict] = field(default_factory=list)
    summary: str = ""
    error: Optional[str] = None


def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


class MeetingRecord:
    def __init__(
        self,
        meeting_id: str,
        title: Optional[str] = None,
        recorded_at: Optional[float] = None,
        duration_s: float = 0.0,
        audio: str = "audio.ogg",
    ) -> None:
        self.meeting_id = meeting_id
        self.title = title
        self.recorded_at = recorded_at
        self.duration_s = duration_s
        self.audio = audio
        self._topics: Dict[str, TopicRecord] = {}

    def add_topic(
        self, topic_id: str, start_s: float, end_s: float, utterances
    ) -> None:
        self._topics[topic_id] = TopicRecord(
            topic_id, start_s, end_s, [dict(u) for u in utterances]
        )

    def mark_failed(
        self, topic_id: str, start_s: float, end_s: float, error: str
    ) -> None:
        self._topics[topic_id] = TopicRecord(
            topic_id, start_s, end_s, error=error
        )

    def add_summary(self, topic_id: str, text: str) -> None:
        if topic_id in self._topics:
            self._topics[topic_id].summary = text

    def ordered(self) -> List[TopicRecord]:
        return sorted(self._topics.values(), key=lambda t: t.start_s)

    def turns(self) -> List[Turn]:
        """Every utterance as a turn for linking, in meeting order. The
        speaker is still the topic's own number until relabel()."""
        out: List[Turn] = []
        for index, topic in enumerate(self.ordered()):
            for u in topic.utterances:
                out.append(
                    Turn(
                        index,
                        u["speaker"],
                        topic.start_s + u["start_s"],
                        topic.start_s + u["end_s"],
                        u.get("embedding"),
                    )
                )
        return out

    def relabel(self, labels: List[int]) -> None:
        """Meeting-wide speaker numbers, in the order turns() gave."""
        utterances = [u for t in self.ordered() for u in t.utterances]
        for utterance, label in zip(utterances, labels):
            utterance["speaker"] = label

    @property
    def speaker_count(self) -> int:
        """How many numbers the record actually holds -- what it can claim,
        not what the uploader said."""
        return len(
            {u["speaker"] for t in self._topics.values() for u in t.utterances}
        )

    def _clock(self, at_s: float) -> str:
        if self.recorded_at is None:
            return f"[{_mmss(at_s)}]"
        wall = time.strftime("%H:%M", time.localtime(self.recorded_at + at_s))
        return f"[{wall} / {_mmss(at_s)}]"

    def lines_for(self, topic_id: str) -> str:
        topic = self._topics.get(topic_id)
        if topic is None:
            return ""
        if topic.error:
            return (
                f"（{_mmss(topic.start_s)}–{_mmss(topic.end_s)} "
                f"這一段未能處理：{topic.error}）"
            )
        return "\n".join(
            f"{self._clock(topic.start_s + u['start_s'])} "
            f"說話人{u['speaker'] + 1}：{u['text']}"
            for u in topic.utterances
        )

    def llm_lines_for(self, topic_id: str) -> str:
        """What the LLM reads: who said what, without the clock.

        Measured on the board (probe_meeting_summary.py): a 1601-character
        topic with timestamps, under a prompt asking to mark time and
        speaker, came back copied line by line, twice -- 3419 characters in
        346 s, past the graph's 175 s. Without timestamps and with the
        labels in simplified characters, the same topic was summarised in
        102 characters and 11 s. Traditional characters in the prompt also
        came back as broken bytes more often (說 as U+FFFD)."""
        topic = self._topics.get(topic_id)
        if topic is None or topic.error:
            return ""
        return "\n".join(
            f"说话人{u['speaker'] + 1}：{u['text']}" for u in topic.utterances
        )

    def to_json(
        self,
        summary: str,
        actions: List[dict],
        actions_error: Optional[str],
        error: Optional[str] = None,
    ) -> dict:
        """record.json. Embeddings stay behind: they exist to link speakers
        and mean nothing to anyone reading the record."""
        return {
            "meeting_id": self.meeting_id,
            "title": self.title,
            "recorded_at": self.recorded_at,
            "duration_s": self.duration_s,
            "audio": self.audio,
            "speaker_count": self.speaker_count,
            "summary": summary,
            "topics": [
                {
                    "id": t.id,
                    "start_s": t.start_s,
                    "end_s": t.end_s,
                    "summary": t.summary,
                    "error": t.error,
                    "utterances": [
                        {k: v for k, v in u.items() if k != "embedding"}
                        for u in t.utterances
                    ],
                }
                for t in self.ordered()
            ],
            "actions": actions,
            "actions_error": actions_error,
            "error": error,
        }
