#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""TTS over the on-board tts_d daemon.

tts_d synthesises a whole WAV per INFER at a hardcoded 22050 Hz. The transport
is 16 kHz G.722 and the RTSA SDK is told its PCM rate once at initialisation
rather than per frame, so this client converts to 16000 before yielding.
"""

import math
import os
import re
import uuid
import wave
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import numpy as np
from scipy.signal import resample_poly
from ten_ai_base.tts2_http import (
    AsyncTTS2HttpClient,
    TTS2HttpResponseEventType,
)
from ten_runtime import AsyncTenEnv

from .config import AmbarellaTTSConfig
from .const import (
    CHUNK_BYTES,
    LOG_CATEGORY_KEY_POINT,
    LOG_CATEGORY_VENDOR,
    NATIVE_SAMPLE_RATE,
    READY_TOKEN,
    SENTENCE_MARKS,
)
from .daemon import DaemonClient, DaemonError

_WHITESPACE = re.compile(r"\s+")


def sanitise(text: str) -> str:
    """Fold every whitespace run into a single space.

    Mandatory, not defensive: the protocol is one command per line with the
    last whitespace token as the output path, so a newline in LLM output would
    end the command early and leave the remainder to be read as a second,
    unparseable command -- desynchronising the pipe for good.
    """
    return _WHITESPACE.sub(" ", text).strip()


def split_text(text: str, max_chars: int) -> List[str]:
    """Split one over-long sentence on punctuation, then hard-split."""
    if len(text) <= max_chars:
        return [text]

    pieces: List[str] = []
    current = ""
    for char in text:
        current += char
        if char in SENTENCE_MARKS and len(current) >= max_chars // 2:
            pieces.append(current.strip())
            current = ""
    if current.strip():
        pieces.append(current.strip())

    hard: List[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            hard.append(piece)
            continue
        for start in range(0, len(piece), max_chars):
            hard.append(piece[start : start + max_chars])
    return [piece for piece in hard if piece]


class AmbarellaTTSClient(AsyncTTS2HttpClient):
    """Drives one resident tts_d process."""

    def __init__(
        self, config: AmbarellaTTSConfig, ten_env: AsyncTenEnv
    ) -> None:
        super().__init__()
        self.config = config
        self.ten_env = ten_env
        self._is_cancelled = False
        self._daemon: Optional[DaemonClient] = None
        self._wav_path = os.path.join(
            config.tmp_dir, f"ambarella_tts_{uuid.uuid4().hex[:8]}.wav"
        )
        self._warned_rate = False
        self._starts = 0
        # Test seam: the stub daemon is a script, so tests prepend its path.
        self._stub_prefix: List[str] = []

    async def warm_up(self) -> None:
        """Load the model in the background, so the first turn does not.

        Never raises: it runs as a detached task, and get() reports the same
        failure through the ERROR event if the load really is broken.
        """
        try:
            await self.start()
        except DaemonError as err:
            self.ten_env.log_error(
                f"tts_d failed to warm up: {err}",
                category=LOG_CATEGORY_VENDOR,
            )

    async def start(self) -> None:
        """Spawn tts_d if it is not already resident.

        Every spawn pays a full model load, so respawns are capped the same
        way the ASR side caps them.
        """
        if self._daemon is not None and self._daemon.alive:
            return
        if self._starts > self.config.restart_max_attempts:
            raise DaemonError(
                "tts_d is dead and the restart cap of "
                f"{self.config.restart_max_attempts} is spent"
            )
        self._starts += 1
        self._daemon = DaemonClient(
            bin_path=self.config.bin_path,
            flags=self._stub_prefix + self.config.daemon_flags(),
            ready_token=READY_TOKEN,
            logger=self.ten_env,
            load_timeout_s=self.config.load_timeout_s,
            quit_timeout_s=self.config.quit_timeout_s,
            log_category=LOG_CATEGORY_VENDOR,
        )
        await self._daemon.start()
        self.ten_env.log_info(
            "tts_d is resident and ready", category=LOG_CATEGORY_KEY_POINT
        )

    async def cancel(self) -> None:
        """Stop yielding audio. Never kill the daemon.

        tts_d's INFER cannot be interrupted, but it only runs ~0.3 s. Killing
        the process on barge-in would cost a full model reload, and barge-in is
        routine in a voice pipeline.
        """
        self.ten_env.log_debug("AmbarellaTTS: cancel() called")
        self._is_cancelled = True

    async def clean(self) -> None:
        if self._daemon is not None:
            await self._daemon.stop()
            self._daemon = None
        if os.path.exists(self._wav_path):
            os.unlink(self._wav_path)

    def get_extra_metadata(self) -> Dict[str, Any]:
        return {
            "speaker_id": self.config.params.get("speaker_id", 0),
            "rand_seed": self.config.params.get("rand_seed", -1),
        }

    async def get(
        self, text: str, request_id: str
    ) -> AsyncIterator[Tuple[Optional[bytes], TTS2HttpResponseEventType]]:
        self._is_cancelled = False

        clean_text = sanitise(text)
        if not clean_text:
            self.ten_env.log_warn(
                f"AmbarellaTTS: empty text for request_id {request_id}"
            )
            yield None, TTS2HttpResponseEventType.END
            return

        try:
            await self.start()
        except DaemonError as err:
            yield str(err).encode("utf-8"), TTS2HttpResponseEventType.ERROR
            yield None, TTS2HttpResponseEventType.END
            return

        for piece in split_text(clean_text, self.config.max_chars):
            if self._is_cancelled:
                yield None, TTS2HttpResponseEventType.FLUSH
                return

            try:
                assert self._daemon is not None
                line = await self._daemon.request(
                    f"INFER {piece} {self._wav_path}",
                    self.config.infer_timeout_s,
                )
            except DaemonError as err:
                self.ten_env.log_error(
                    f"vendor_error: {err}", category=LOG_CATEGORY_VENDOR
                )
                yield str(err).encode("utf-8"), TTS2HttpResponseEventType.ERROR
                yield None, TTS2HttpResponseEventType.END
                return

            if line.startswith("ERR"):
                self.ten_env.log_error(
                    f"vendor_error: {line}", category=LOG_CATEGORY_VENDOR
                )
                yield line.encode("utf-8"), TTS2HttpResponseEventType.ERROR
                yield None, TTS2HttpResponseEventType.END
                return

            try:
                pcm = self._read_and_resample()
            except (OSError, wave.Error) as err:
                yield str(err).encode("utf-8"), TTS2HttpResponseEventType.ERROR
                yield None, TTS2HttpResponseEventType.END
                return

            for start in range(0, len(pcm), CHUNK_BYTES):
                if self._is_cancelled:
                    yield None, TTS2HttpResponseEventType.FLUSH
                    return
                yield pcm[
                    start : start + CHUNK_BYTES
                ], TTS2HttpResponseEventType.RESPONSE

        yield None, TTS2HttpResponseEventType.END

    def _read_and_resample(self) -> bytes:
        """Read tts_d's WAV back and convert it to the declared rate."""
        with wave.open(self._wav_path, "rb") as source:
            header_rate = source.getframerate()
            frames = source.readframes(source.getnframes())

        if header_rate != NATIVE_SAMPLE_RATE and not self._warned_rate:
            self._warned_rate = True
            # A different rate means the binary or the model was swapped, and
            # a fixed 320/441 ratio would then be wrong.
            self.ten_env.log_error(
                f"tts_d wrote {header_rate} Hz, not the expected "
                f"{NATIVE_SAMPLE_RATE} Hz; the resampling ratio is being "
                "derived from the header instead",
                category=LOG_CATEGORY_VENDOR,
            )

        target = self.config.output_sample_rate
        if header_rate == target:
            return frames

        samples = np.frombuffer(frames, dtype=np.int16)
        if samples.size == 0:
            return b""

        divisor = math.gcd(target, header_rate)
        up = target // divisor
        down = header_rate // divisor
        # resample_poly applies an FIR anti-aliasing filter. np.interp, used
        # elsewhere in this repo, does not -- and this is a downsample, so
        # without the filter the 8-11 kHz content folds back audibly.
        resampled = resample_poly(samples.astype(np.float32), up, down)
        clipped = np.clip(resampled, -32768.0, 32767.0)
        return clipped.astype(np.int16).tobytes()
