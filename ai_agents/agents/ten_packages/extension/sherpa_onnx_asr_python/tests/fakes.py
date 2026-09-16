#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A stand-in for sherpa-onnx's streaming recogniser.

Faked to its real contract -- create_stream, accept_waveform, is_ready,
decode_stream, get_result, is_endpoint, reset -- so a test that passes here
means the adapter drives the engine the way the engine expects. The model is
300 MB and lives on the board.
"""

import json
import threading
import time
from typing import List, Optional


class FakeStream:
    def __init__(self) -> None:
        self.samples: List[float] = []
        self.finished = False

    def accept_waveform(self, sample_rate: int, samples) -> None:
        self.rate = sample_rate
        self.samples.extend(list(samples))

    def input_finished(self) -> None:
        self.finished = True


class FakeRecogniser:
    """Emits a scripted sequence of (text, is_endpoint) as decoding proceeds.

    ``decode_delay`` puts real time inside decode_stream so a test can tell
    whether it is being called on the event loop.
    """

    def __init__(
        self,
        script: Optional[List[tuple]] = None,
        decode_delay: float = 0.0,
        step_samples: int = 320,  # 20 ms at 16 kHz -- one frame
    ) -> None:
        # (text after this decode, whether an endpoint follows)
        self.script = list(script or [])
        self.decode_delay = decode_delay
        self.step_samples = step_samples

        self.streams: List[FakeStream] = []
        self.decode_thread_ids: List[int] = []
        self.decodes = 0
        self.resets = 0
        self._text = ""
        self._endpoint = False

    def create_stream(self) -> FakeStream:
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def is_ready(self, stream) -> bool:
        # The real engine is ready once enough audio has arrived for another
        # step, not whenever it has something left to say. Tying readiness to
        # unconsumed samples keeps one decode per frame, which is what the
        # timing assertions depend on.
        return bool(self.script) and len(stream.samples) >= self.step_samples

    def decode_stream(self, stream) -> None:
        if self.decode_delay:
            time.sleep(self.decode_delay)
        self.decode_thread_ids.append(threading.get_ident())
        self.decodes += 1
        del stream.samples[: self.step_samples]
        if self.script:
            self._text, self._endpoint = self.script.pop(0)

    def get_result(self, _stream) -> str:
        return self._text

    def is_endpoint(self, _stream) -> bool:
        return self._endpoint

    def reset(self, _stream) -> None:
        self.resets += 1
        self._text = ""
        self._endpoint = False


class FakeTenEnv:
    def __init__(self, properties: Optional[dict] = None) -> None:
        self.lines: List[str] = []
        self.properties = properties or {}

    async def get_property_to_json(self, _path: str):
        return json.dumps(self.properties), None

    async def get_property_bool(self, name: str):
        if name not in self.properties:
            return False, "not found"
        return bool(self.properties[name]), None

    def _record(self, message: str, **_kwargs) -> None:
        self.lines.append(message)

    log_debug = _record
    log_info = _record
    log_warn = _record
    log_error = _record
