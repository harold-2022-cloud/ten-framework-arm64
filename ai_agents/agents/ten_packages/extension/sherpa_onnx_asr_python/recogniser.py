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
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

import numpy as np

from .config import SherpaOnnxASRConfig
from .const import LOG_CATEGORY_KEY_POINT, SAMPLE_RATE

BYTES_PER_SAMPLE = 2

# How far the decoder may fall behind the audio before it is worth saying so,
# and how far it has to recover before the next time counts as new. Hysteresis
# rather than a level, so draining the start-up buffer is two lines and not one
# per frame.
BEHIND_WARN_MS = 500
BEHIND_CLEAR_MS = 200


@dataclass
class Transcript:
    """One thing to tell the graph. Mapped to ASRResult by the extension."""

    text: str
    final: bool
    start_ms: int
    duration_ms: int


def whole_samples(pcm: bytes) -> bytes:
    """The part of a frame that is complete samples.

    A frame can arrive cut mid-sample. Dropping the stray byte keeps the
    stream going; raising would end the turn over one byte. The dump goes
    through here too, so what is written is exactly what was recognised and a
    byte offset in the file converts to the start_ms of a transcript.
    """
    return pcm[: len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)]


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    """Scale 16-bit PCM to the [-1, 1] floats accept_waveform expects."""
    samples = np.frombuffer(whole_samples(pcm), dtype=np.int16)
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
        clock: Callable[[], float] = time.monotonic,
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

        # What the last run's log could not answer was in here and never said:
        # which rule ended an utterance, and how far behind the audio the
        # decoder was running. Both had to be reconstructed by arithmetic
        # across six lines, which is not a thing to do twice.
        self._clock = clock
        self._clock_start: Optional[float] = None
        self._last_text_change_ms = 0
        self._decodes = 0
        self._behind = False
        # Filled on the worker thread, drained on the loop: ten_env is not
        # ours to call from inside run_in_executor.
        self._notes: List[str] = []

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Load the model and open a stream. Loading is off the event loop."""
        async with self._lock:
            if self.engine is not None:
                return
            # Before the load, not after: the audio clock is measured against
            # this, and the frames that arrive while the model loads are the
            # ones that put the decoder behind in the first place.
            self._clock_start = self._clock()
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
        self._clock_start = None
        self._last_text_change_ms = 0
        self._decodes = 0
        self._behind = False
        self._notes.clear()

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

        out = await self._drain()
        self._report_lag()
        self._flush_notes()
        return out

    @property
    def _lag_ms(self) -> int:
        """How far the audio accepted trails the clock since start().

        Relative, not absolute: capture may begin a little before or after the
        extension does, so the useful reading is the shape -- large while the
        buffer that built up during the model load drains, near zero once the
        decoder is keeping up.
        """
        if self._clock_start is None:
            return 0
        wall_ms = (self._clock() - self._clock_start) * 1000
        # Rounded, not truncated: a difference that is exactly 1020 ms comes
        # out of the float subtraction a hair under, and int() would log 999.
        return round(wall_ms - self._elapsed_ms)

    def _report_lag(self) -> None:
        lag = self._lag_ms
        if not self._behind and lag >= BEHIND_WARN_MS:
            self._behind = True
            self.ten_env.log_warn(
                f"decoder {lag} ms behind the audio "
                f"({self._elapsed_ms} ms accepted)"
            )
        elif self._behind and lag <= BEHIND_CLEAR_MS:
            self._behind = False
            self.ten_env.log_info(
                f"decoder caught up, {lag} ms behind "
                f"({self._elapsed_ms} ms accepted)",
                category=LOG_CATEGORY_KEY_POINT,
            )

    def _flush_notes(self) -> None:
        for note in self._notes:
            self.ten_env.log_info(note, category=LOG_CATEGORY_KEY_POINT)
        self._notes.clear()

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
            self._decodes += 1
            text = engine.get_result(stream).strip()
            endpoint = engine.is_endpoint(stream)

            if endpoint:
                if text:
                    out.append(self._make(text, final=True))
                # sherpa-onnx says an endpoint happened, never which rule
                # said so. The trailing silence is the thing that tells them
                # apart -- rule1 and rule2 are exactly that, and rule3 shows
                # up as a long utterance instead -- so measure it here rather
                # than reconstruct it from timestamps afterwards.
                self._note_endpoint(text)
                # Reset even with no text: silence long enough to end an
                # utterance still ends it, and the next one must start clean.
                engine.reset(stream)
                self._last_emitted = ""
                self._utterance_start_ms = self._elapsed_ms
                self._last_text_change_ms = self._elapsed_ms
                continue

            if text and text != self._last_emitted:
                self._last_emitted = text
                self._last_text_change_ms = self._elapsed_ms
                out.append(self._make(text, final=False))

        return out

    def _note_endpoint(self, text: str) -> None:
        # Runs on the worker thread; the line is logged once back on the loop.
        trailing = max(0, self._elapsed_ms - self._last_text_change_ms)
        utterance = max(0, self._elapsed_ms - self._utterance_start_ms)
        self._notes.append(
            f"endpoint after {trailing} ms of trailing silence: "
            f"utterance={utterance} ms decodes={self._decodes} "
            f"lag={self._lag_ms} ms text={text!r}"
        )
        self._decodes = 0

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
        self._flush_notes()

        pending = self._last_emitted
        if pending:
            self.ten_env.log_info(
                f"finalize took the utterance before any endpoint: "
                f"{self._elapsed_ms - self._utterance_start_ms} ms of audio, "
                f"{max(0, self._elapsed_ms - self._last_text_change_ms)} ms of "
                f"trailing silence, text={pending!r}",
                category=LOG_CATEGORY_KEY_POINT,
            )
            out.append(self._make(pending, final=True))
            self._last_emitted = ""

        # A new stream rather than reset(): input_finished() is terminal, and
        # a finalized stream accepts no more audio.
        self.stream = self.engine.create_stream()
        self._utterance_start_ms = self._elapsed_ms
        self._last_text_change_ms = self._elapsed_ms
        return out
