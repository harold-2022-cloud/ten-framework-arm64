#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""TTS through a sherpa-onnx VITS voice, on the board's own CPU.

``OfflineTts.generate()`` is a synchronous C++ call that blocks its thread for
the whole utterance and hands audio back through a callback, one chunk per
sentence, as it goes. This client keeps both facts inside itself: the call runs
in a worker thread, and each chunk is converted and queued the moment it
arrives, so a reply starts playing while its later sentences are still being
synthesised.
"""

import asyncio
import glob
import os
import re
from typing import Any, AsyncIterator, Callable, Dict, Optional, Tuple

import numpy as np
from ten_ai_base.tts2_http import (
    AsyncTTS2HttpClient,
    TTS2HttpResponseEventType,
)

from .audio import float_to_pcm16, resample_pcm16
from .config import SherpaOnnxTTSConfig
from .const import (
    CALLBACK_CONTINUE,
    CALLBACK_STOP,
    CLAUSE_MARKS,
    FRAME_MS,
    LOG_CATEGORY_KEY_POINT,
    LOG_CATEGORY_VENDOR,
)

_WHITESPACE = re.compile(r"\s+")


def sanitise(text: str) -> str:
    """Fold whitespace runs into single spaces and trim the result."""
    return _WHITESPACE.sub(" ", text).strip()


def split_for_latency(text: str, max_chars: int) -> list:
    """Break a long run of clauses so the first audio does not wait.

    The engine ends a sentence only at 。！？, so a line written with commas
    is one callback however long it is. Splitting on clause punctuation gives
    the listener the first clause while the rest is still being synthesised.

    Never splits mid-clause: a cut inside a word changes how it is read, and
    a clause with no punctuation is returned whole however long it is.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    # Cut at every clause mark first, keeping the mark with the clause it
    # ends so the phrasing survives.
    clauses = []
    current = ""
    for char in text:
        current += char
        if char in CLAUSE_MARKS:
            clauses.append(current)
            current = ""
    if current:
        clauses.append(current)
    if len(clauses) < 2:
        # Nothing to cut on. A cut inside a word changes how it is read.
        return [text]

    # The first piece is the first clause alone, however short: it is the one
    # the listener is waiting for, and everything after it is synthesised
    # while that clause plays. Later pieces are packed up to the limit,
    # because by then the only thing that matters is keeping ahead of
    # playback, and fewer calls do that better.
    pieces = [clauses[0]]
    current = ""
    for clause in clauses[1:]:
        if current and len(current) + len(clause) > max_chars:
            pieces.append(current)
            current = clause
        else:
            current += clause
    if current:
        pieces.append(current)
    return pieces


def load_vits_engine(config: SherpaOnnxTTSConfig):
    """Build an ``OfflineTts`` from a sherpa-onnx voice bundle.

    Imported here rather than at module scope so the rest of this file, and
    its tests, do not need the 40 MB native library present.
    """
    import sherpa_onnx  # pylint: disable=import-outside-toplevel

    voice_dir = config.voice_dir
    # The model file is named after the voice, so find it rather than assume.
    models = sorted(glob.glob(os.path.join(voice_dir, "*.onnx")))
    if not models:
        raise FileNotFoundError(f"no *.onnx model in {voice_dir}")
    tokens = os.path.join(voice_dir, "tokens.txt")
    if not os.path.isfile(tokens):
        raise FileNotFoundError(f"no tokens.txt in {voice_dir}")

    # Which of these a bundle carries depends on its phonemiser: the Mandarin
    # Piper voice has espeak-ng-data and neither of the other two. Absent
    # ones are passed as empty strings, which is how sherpa-onnx spells "not
    # used".
    data_dir = os.path.join(voice_dir, "espeak-ng-data")
    dict_dir = os.path.join(voice_dir, "dict")
    lexicons = sorted(glob.glob(os.path.join(voice_dir, "lexicon*.txt")))

    vits = sherpa_onnx.OfflineTtsVitsModelConfig(
        model=models[0],
        tokens=tokens,
        data_dir=data_dir if os.path.isdir(data_dir) else "",
        dict_dir=dict_dir if os.path.isdir(dict_dir) else "",
        lexicon=lexicons[0] if lexicons else "",
    )
    return sherpa_onnx.OfflineTts(
        sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=vits,
                num_threads=config.num_threads,
                provider="cpu",
            ),
        )
    )


class SherpaOnnxTTSClient(AsyncTTS2HttpClient):
    """Drives one loaded VITS voice."""

    def __init__(
        self,
        config: SherpaOnnxTTSConfig,
        ten_env,
        load_engine: Optional[Callable[[SherpaOnnxTTSConfig], Any]] = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.ten_env = ten_env
        self.engine = None
        # Test seam: the voice is 67 MB and lives on the board.
        self._load_engine = load_engine or load_vits_engine
        self._load_lock = asyncio.Lock()
        self._is_cancelled = False

    # --- lifecycle ---------------------------------------------------------

    async def warm_up(self) -> None:
        """Load the voice before the first turn needs it.

        Never raises: this runs as a detached task, so there is nobody to
        catch it. get() reloads and reports the same failure through ERROR,
        which is the path that can reach the pipeline.
        """
        try:
            await self._ensure_engine()
        except Exception as err:  # pylint: disable=broad-except
            self.ten_env.log_error(
                f"sherpa-onnx voice failed to load: {err}",
                category=LOG_CATEGORY_VENDOR,
            )

    async def _ensure_engine(self):
        """Load the voice once, off the event loop.

        The lock matters because warm_up() and the first get() can overlap:
        without it both would load, and two copies of the model would sit in
        memory until one was collected.
        """
        async with self._load_lock:
            if self.engine is not None:
                return self.engine
            engine = await asyncio.get_running_loop().run_in_executor(
                None, self._load_engine, self.config
            )
            self.engine = engine
            self.ten_env.log_info(
                f"sherpa-onnx voice loaded from {self.config.voice_dir}, "
                f"{engine.sample_rate} Hz",
                category=LOG_CATEGORY_KEY_POINT,
            )
            return engine

    async def cancel(self) -> None:
        """Stop the synthesis in flight. The voice stays loaded.

        Barge-in is routine and reloading the voice costs seconds, so this
        only raises the flag the generation callback reads.
        """
        self.ten_env.log_debug("SherpaOnnxTTS: cancel() called")
        self._is_cancelled = True

    async def clean(self) -> None:
        self.engine = None

    def get_extra_metadata(self) -> Dict[str, Any]:
        return {
            "voice": os.path.basename(self.config.voice_dir.rstrip("/")),
            "speaker_id": self.config.speaker_id,
        }

    # --- synthesis ---------------------------------------------------------

    async def get(
        self, text: str, request_id: str
    ) -> AsyncIterator[Tuple[Optional[bytes], TTS2HttpResponseEventType]]:
        self._is_cancelled = False

        clean_text = sanitise(text)
        if not clean_text:
            # main_control closes every turn with an empty string, so this is
            # the normal path, not a fault.
            self.ten_env.log_debug(
                f"SherpaOnnxTTS: empty text for request_id {request_id}"
            )
            yield None, TTS2HttpResponseEventType.END
            return

        try:
            engine = await self._ensure_engine()
        except Exception as err:  # pylint: disable=broad-except
            self.ten_env.log_error(
                f"vendor_error: {err}", category=LOG_CATEGORY_VENDOR
            )
            yield str(err).encode("utf-8"), TTS2HttpResponseEventType.ERROR
            yield None, TTS2HttpResponseEventType.END
            return

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        source_rate = int(engine.sample_rate)
        target_rate = int(self.config.output_sample_rate)
        # The base class turns each yield into one AudioFrame, so the size of
        # a yield is the size of a frame. A sentence is seconds of audio and
        # would make one of a few hundred kilobytes; the rest of the pipeline
        # is built around 20 ms. Framing here also bounds barge-in: without
        # it, a sentence already synthesised is delivered whole however late
        # the interruption arrives.
        frame_bytes = max(2, target_rate * 2 * FRAME_MS // 1000)
        failure: list = []

        def on_chunk(samples: np.ndarray, _progress: float) -> int:
            # Runs on the worker thread. Conversion happens here rather than
            # on the event loop, which is the whole point of the thread.
            pcm = resample_pcm16(
                float_to_pcm16(samples), source_rate, target_rate
            )
            loop.call_soon_threadsafe(queue.put_nowait, pcm)
            # Zero stops generation and non-zero continues it. That is the
            # reverse of what generate()'s docstring says; following the
            # docstring truncates every reply to its first sentence, with no
            # error anywhere. Measured against sherpa-onnx 1.13.8 and pinned
            # by tests/test_engine_contract.py.
            return CALLBACK_STOP if self._is_cancelled else CALLBACK_CONTINUE

        pieces = split_for_latency(
            clean_text, self.config.max_chars_before_split
        )

        def synthesise() -> None:
            try:
                for piece in pieces:
                    if self._is_cancelled:
                        break
                    engine.generate(
                        piece,
                        sid=self.config.speaker_id,
                        speed=self.config.speed,
                        callback=on_chunk,
                    )
            except Exception as err:  # pylint: disable=broad-except
                failure.append(err)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        worker = loop.run_in_executor(None, synthesise)
        # Sentences do not divide evenly into frames; the remainder waits
        # here for the next sentence rather than being padded or dropped,
        # either of which would be audible over a long reply.
        pending = bytearray()
        # Whether this generator reached the end of the stream, as opposed to
        # being stopped. It cannot be asked of the worker: the sentinel is
        # queued from inside synthesise(), and the executor only marks the
        # future done after that function returns, so a normal completion is
        # still "not done" at the moment the consumer sees the sentinel.
        # Reading it there reported every finished turn as an interruption.
        drained = False
        try:
            while True:
                pcm = await queue.get()
                if pcm is None:
                    drained = True
                    break
                pending.extend(pcm)
                while len(pending) >= frame_bytes and not self._is_cancelled:
                    yield bytes(
                        pending[:frame_bytes]
                    ), TTS2HttpResponseEventType.RESPONSE
                    del pending[:frame_bytes]
                if self._is_cancelled:
                    break
            if pending and not self._is_cancelled:
                yield bytes(pending), TTS2HttpResponseEventType.RESPONSE
        finally:
            # Whoever stops reading stops the synthesis: a consumer that
            # breaks out of this generator would otherwise leave a thread
            # synthesising a reply nobody will hear.
            if not drained:
                self._is_cancelled = True
            await worker

        if failure:
            self.ten_env.log_error(
                f"vendor_error: {failure[0]}", category=LOG_CATEGORY_VENDOR
            )
            yield str(failure[0]).encode(
                "utf-8"
            ), TTS2HttpResponseEventType.ERROR
            yield None, TTS2HttpResponseEventType.END
            return

        if self._is_cancelled:
            yield None, TTS2HttpResponseEventType.FLUSH
            return
        yield None, TTS2HttpResponseEventType.END
