#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Stand-ins for the parts of the world the unit tests must not touch.

The engine is faked rather than loaded: the Mandarin voice is 67 MB and is on
the board, not in this checkout. What is faked is exactly sherpa-onnx's
contract -- a ``sample_rate`` attribute and a ``generate()`` that calls back
per sentence and honours the callback's return value -- so a test that passes
here means the adapter holds up its end of that contract.
"""

import threading
import time
from types import SimpleNamespace
from typing import Callable, List, Optional

import numpy as np


class FakeTenEnv:
    """Collects log lines so a test can assert what was reported."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def _record(self, message: str, **_kwargs) -> None:
        self.lines.append(message)

    log_debug = _record
    log_info = _record
    log_warn = _record
    log_error = _record


class FakeEngine:
    """A sherpa-onnx ``OfflineTts`` with the timing made observable.

    ``chunk_delay`` puts real time between callbacks so a test can tell
    whether audio is being forwarded as it arrives or collected first.
    """

    def __init__(
        self,
        sample_rate: int = 22050,
        num_chunks: int = 3,
        chunk_delay: float = 0.0,
        samples_per_chunk: int = 160,
    ) -> None:
        self.sample_rate = sample_rate
        self.num_chunks = num_chunks
        self.chunk_delay = chunk_delay
        self.samples_per_chunk = samples_per_chunk

        self.texts: List[str] = []
        self.thread_ids: List[int] = []
        self.returns: List[int] = []
        self.chunks_emitted = 0
        self.generating = False

    def generate(
        self,
        text: str,
        sid: int = 0,
        speed: float = 1.0,
        callback: Optional[Callable[[np.ndarray, float], int]] = None,
    ):
        self.texts.append(text)
        self.thread_ids.append(threading.get_ident())
        self.generating = True
        emitted = []
        try:
            for index in range(self.num_chunks):
                if self.chunk_delay:
                    time.sleep(self.chunk_delay)
                chunk = np.full(
                    self.samples_per_chunk,
                    (index + 1) / 10.0,
                    dtype=np.float32,
                )
                emitted.append(chunk)
                self.chunks_emitted += 1
                if callback is None:
                    continue
                verdict = callback(chunk, (index + 1) / self.num_chunks)
                self.returns.append(verdict)
                # Zero stops, non-zero continues -- the inverse of what
                # generate()'s docstring claims, and the reason this fake
                # asserts on the value rather than ignoring it.
                if verdict == 0:
                    break
        finally:
            self.generating = False
        joined = (
            np.concatenate(emitted)
            if emitted
            else np.zeros(0, dtype=np.float32)
        )
        return SimpleNamespace(samples=joined, sample_rate=self.sample_rate)
