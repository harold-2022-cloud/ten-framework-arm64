#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""One meeting's audio, cut into topic-sized files.

The recorder knows nothing about meetings or topics. It is told to open a
segment and told to close one; what makes a segment is somebody else's
judgement.
"""

import os
import time
from dataclasses import dataclass
from typing import Optional

BYTES_PER_SAMPLE = 2
SAMPLE_RATE = 16000


@dataclass
class Segment:
    """One closed file, and what the rest of the pipeline needs to place it."""

    path: str
    started_at: float
    duration_s: float
    bytes_written: int


def whole_samples(pcm: bytes) -> bytes:
    """The part of a frame that is complete samples.

    A frame can arrive cut mid-sample. Dropping the stray byte keeps the
    stream aligned; keeping it shifts every later sample by half a sample,
    which reaches the transcript as noise nobody can trace back to here.
    """
    return pcm[: len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)]


class SegmentWriter:
    """Raw PCM16 at the rate it arrived. No header: what reads these is our
    own transcriber, and a byte offset is then a position in the segment."""

    def __init__(self, root: str, sample_rate: int = SAMPLE_RATE) -> None:
        self.root = root
        self.sample_rate = sample_rate
        self._file = None
        self._path = ""
        self._started_at = 0.0
        self._bytes = 0

    @property
    def is_open(self) -> bool:
        return self._file is not None

    def open(self, started_at: float) -> str:
        os.makedirs(self.root, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(started_at))
        self._path = os.path.join(self.root, f"segment_{stamp}.pcm")
        self._file = open(self._path, "wb")
        self._started_at = started_at
        self._bytes = 0
        return self._path

    def write(self, pcm: bytes) -> int:
        if self._file is None:
            return 0
        usable = whole_samples(pcm)
        self._file.write(usable)
        self._bytes += len(usable)
        return len(usable)

    def close(self) -> Optional[Segment]:
        if self._file is None:
            return None
        self._file.close()
        self._file = None
        return Segment(
            path=self._path,
            started_at=self._started_at,
            duration_s=self._bytes / BYTES_PER_SAMPLE / self.sample_rate,
            bytes_written=self._bytes,
        )
