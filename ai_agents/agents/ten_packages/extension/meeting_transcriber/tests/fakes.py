#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Stand-ins for the two sherpa-onnx engines.

Faked to the contract used in transcriber.py -- process() returning something
with sort_by_start_time(), and a recogniser built per utterance -- so a test
that passes here means the adapter drives them the way they expect. The models
are 47 MB and live on the board.
"""

from dataclasses import dataclass
from typing import List


@dataclass
class FakeSegment:
    start: float
    end: float
    speaker: int


class FakeDiarizationResult:
    def __init__(self, segments):
        self._segments = segments

    def sort_by_start_time(self):
        return list(self._segments)


class FakeDiarizer:
    """Returns a scripted segmentation and records what it was given."""

    def __init__(self, segments: List[FakeSegment]) -> None:
        self.segments = segments
        self.sample_counts: List[int] = []

    def process(self, samples, callback=None):
        self.sample_counts.append(len(samples))
        return FakeDiarizationResult(self.segments)


class FakeStream:
    def __init__(self):
        self.samples = []

    def accept_waveform(self, sample_rate, samples):
        self.rate = sample_rate
        self.samples.extend(list(samples))


class FakeRecogniser:
    """Hands back one scripted text per decode, in order."""

    def __init__(self, texts: List[str]) -> None:
        self.texts = list(texts)
        self.decoded = 0

    def create_stream(self):
        return FakeStream()

    def decode_stream(self, stream):
        self.decoded += 1

    def get_result(self, stream):
        class _R:
            text = self.texts.pop(0) if self.texts else ""

        return _R()


class FakeTenEnv:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def _record(self, message: str, **_kwargs) -> None:
        self.lines.append(message)

    log_debug = _record
    log_info = _record
    log_warn = _record
    log_error = _record
