#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Streaming transcription through a sherpa-onnx Zipformer, on the CPU.

The engine decodes in a synchronous C++ call and reports the text so far after
every step. This class keeps that off the event loop, and turns the stream of
repeated partials into the events the graph actually wants: text when it
changed, one final per utterance.
"""

import asyncio
import glob
import os
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

import numpy as np

from .config import SherpaOnnxASRConfig
from .const import LOG_CATEGORY_KEY_POINT, SAMPLE_RATE

BYTES_PER_SAMPLE = 2


@dataclass
class Transcript:
    """One thing to tell the graph. Mapped to ASRResult by the extension."""

    text: str
    final: bool
    start_ms: int
    duration_ms: int


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    """Scale 16-bit PCM to the [-1, 1] floats accept_waveform expects.

    A frame can arrive cut mid-sample. Dropping the stray byte keeps the
    stream going; raising would end the turn over one byte.
    """
    usable = len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)
    samples = np.frombuffer(pcm[:usable], dtype=np.int16)
    return (samples.astype(np.float32) / 32768.0).copy()


def load_zipformer_engine(config: SherpaOnnxASRConfig):
    """Build an OnlineRecognizer from a sherpa-onnx transducer bundle.

    Imported here rather than at module scope so the rest of this file, and
    its tests, do not need the native library present.
    """
    import sherpa_onnx  # pylint: disable=import-outside-toplevel

    model_dir = config.model_dir

    def one(pattern: str) -> str:
        # The bundles ship int8 and float variants side by side; prefer the
        # int8 encoder, which is what the board's measurement was taken with.
        hits = sorted(glob.glob(os.path.join(model_dir, pattern)))
        if not hits:
            raise FileNotFoundError(f"no {pattern} in {model_dir}")
        int8 = [h for h in hits if "int8" in os.path.basename(h)]
        return (int8 or hits)[0]

    tokens = os.path.join(model_dir, "tokens.txt")
    if not os.path.isfile(tokens):
        raise FileNotFoundError(f"no tokens.txt in {model_dir}")

    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=tokens,
        encoder=one("encoder-*.onnx"),
        decoder=one("decoder-*.onnx"),
        joiner=one("joiner-*.onnx"),
        num_threads=config.num_threads,
        sample_rate=SAMPLE_RATE,
        provider="cpu",
        enable_endpoint_detection=config.enable_endpoint_detection,
        rule1_min_trailing_silence=config.rule1_min_trailing_silence,
        rule2_min_trailing_silence=config.rule2_min_trailing_silence,
        rule3_min_utterance_length=config.rule3_min_utterance_length,
    )


class SherpaOnnxRecogniser:
    """Drives one loaded streaming model."""

    def __init__(
        self,
        config: SherpaOnnxASRConfig,
        ten_env,
        load_engine: Optional[Callable[[SherpaOnnxASRConfig], Any]] = None,
    ) -> None:
        self.config = config
        self.ten_env = ten_env
        self.engine = None
        self.stream = None
        # Test seam: the model is 300 MB and lives on the board.
        self._load_engine = load_engine or load_zipformer_engine
        self._lock = asyncio.Lock()

        self._last_emitted = ""
        self._samples_accepted = 0
        self._utterance_start_ms = 0

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Load the model and open a stream. Loading is off the event loop."""
        async with self._lock:
            if self.engine is not None:
                return
            engine = await asyncio.get_running_loop().run_in_executor(
                None, self._load_engine, self.config
            )
            self.engine = engine
            self.stream = engine.create_stream()
            self.ten_env.log_info(
                f"sherpa-onnx model loaded from {self.config.model_dir}",
                category=LOG_CATEGORY_KEY_POINT,
            )

    async def stop(self) -> None:
        self.engine = None
        self.stream = None
        self._last_emitted = ""
        self._samples_accepted = 0
        self._utterance_start_ms = 0

    def is_running(self) -> bool:
        return self.engine is not None

    # --- recognition -------------------------------------------------------

    @property
    def _elapsed_ms(self) -> int:
        # From the audio accepted, not the clock: the buffer runs ahead of or
        # behind real time while the model loads, and a wall-clock stamp would
        # drift against the transcript.
        return int(self._samples_accepted * 1000 / SAMPLE_RATE)

    async def accept(self, pcm: bytes) -> List[Transcript]:
        """Feed one frame and return whatever that made the engine say."""
        if self.engine is None or self.stream is None:
            return []

        samples = pcm16_to_float32(pcm)
        if samples.size == 0:
            return []
        self.stream.accept_waveform(SAMPLE_RATE, samples)
        self._samples_accepted += samples.size

        return await self._drain()

    async def _drain(self) -> List[Transcript]:
        """Decode while the engine has enough, collecting what changed."""
        return await asyncio.get_running_loop().run_in_executor(
            None, self._decode_until_not_ready
        )

    def _decode_until_not_ready(self) -> List[Transcript]:
        # Runs on a worker thread. decode_stream blocks for the length of one
        # step; at the measured real-time factor of 0.2 that is milliseconds,
        # but it is still C++ holding a thread.
        out: List[Transcript] = []
        engine, stream = self.engine, self.stream
        if engine is None or stream is None:
            return out

        while engine.is_ready(stream):
            engine.decode_stream(stream)
            text = engine.get_result(stream).strip()
            endpoint = engine.is_endpoint(stream)

            if endpoint:
                if text:
                    out.append(self._make(text, final=True))
                # Reset even with no text: silence long enough to end an
                # utterance still ends it, and the next one must start clean.
                engine.reset(stream)
                self._last_emitted = ""
                self._utterance_start_ms = self._elapsed_ms
                continue

            if text and text != self._last_emitted:
                self._last_emitted = text
                out.append(self._make(text, final=False))

        return out

    def _make(self, text: str, final: bool) -> Transcript:
        start = self._utterance_start_ms
        return Transcript(
            text=text,
            final=final,
            start_ms=start,
            duration_ms=max(0, self._elapsed_ms - start),
        )

    async def finalize(self) -> List[Transcript]:
        """Close the input and emit what is left as final.

        main_control ends a turn without waiting for silence, so an utterance
        can be complete with no endpoint behind it. Without this the last
        thing said would never reach the graph.
        """
        if self.engine is None or self.stream is None:
            return []

        self.stream.input_finished()
        out = await self._drain()

        pending = self._last_emitted
        if pending:
            out.append(self._make(pending, final=True))
            self._last_emitted = ""

        # A new stream rather than reset(): input_finished() is terminal, and
        # a finalized stream accepts no more audio.
        self.stream = self.engine.create_stream()
        self._utterance_start_ms = self._elapsed_ms
        return out
