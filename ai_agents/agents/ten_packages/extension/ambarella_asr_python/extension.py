#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import asyncio
import os
import uuid
import wave
from typing import List, Optional

from typing_extensions import override

from ten_ai_base.asr import (
    ASRBufferConfig,
    ASRBufferConfigModeKeep,
    ASRResult,
    AsyncASRBaseExtension,
)
from ten_ai_base.message import (
    ModuleError,
    ModuleErrorCode,
    ModuleErrorVendorInfo,
)
from ten_runtime import AsyncTenEnv, AudioFrame

from .config import AmbarellaASRConfig
from .const import (
    BYTES_PER_SECOND,
    LOG_CATEGORY_KEY_POINT,
    LOG_CATEGORY_VENDOR,
    MAX_BUFFER_BYTES,
    MODULE_NAME_ASR,
    NO_SPEECH_ERR,
    READY_TOKEN,
    SAMPLE_RATE,
)
from .daemon import DaemonClient, DaemonError

# asr_d treats these as "the user said nothing", which is not a failure.
_SILENCE_LINES = (NO_SPEECH_ERR, "ERR no_audio", "ERR audio_too_short")


class AmbarellaASRExtension(AsyncASRBaseExtension):
    """ASR over the on-board asr_d daemon.

    Unlike whisper_stt_python, inference happens only in finalize(): asr_d has
    no partial results, so transcribing a rolling window would burn VP time and
    chop sentences with nothing to stitch them back together.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: Optional[AmbarellaASRConfig] = None
        self.daemon: Optional[DaemonClient] = None
        self._buffer = bytearray()
        self._wav_path = ""
        self._start_task: Optional[asyncio.Task] = None
        self._restart_task: Optional[asyncio.Task] = None
        self._restarts = 0
        self._elapsed_ms = 0
        # Serialises _infer_buffer() turns (and the daemon swap that a
        # restart performs) against each other: send_audio() and finalize()
        # can be driven from concurrent asyncio tasks (audio consumer vs.
        # on_data), and both the shared WAV path and the restart bookkeeping
        # are only safe under one turn at a time.
        self._infer_lock = asyncio.Lock()
        # Test seam: the stub daemon is a script, so tests prepend its path.
        self._stub_prefix: List[str] = []

    @override
    def vendor(self) -> str:
        return "ambarella"

    @override
    def input_audio_sample_rate(self) -> int:
        # 16000 is compiled into asr_d and matches what RTC hands us after it
        # decodes G.722, so nothing is resampled on this path.
        return SAMPLE_RATE

    @override
    def buffer_strategy(self) -> ASRBufferConfig:
        # Agree with the daemon's own 30-second window, and catch the frames
        # that arrive while the model is still loading.
        return ASRBufferConfigModeKeep(byte_limit=MAX_BUFFER_BYTES)

    @override
    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)
        config_json, _ = await ten_env.get_property_to_json("")
        try:
            self.config = AmbarellaASRConfig.model_validate_json(config_json)
        except Exception as err:  # pylint: disable=broad-except
            ten_env.log_error(f"invalid ambarella_asr config: {err}")
            self.config = AmbarellaASRConfig()
            await self._fatal(str(err))
            return

        if not self.config.bin_path or not self.config.model_dir:
            await self._fatal("both bin_path and model_dir are required")
            return

        self._wav_path = os.path.join(
            self.config.tmp_dir,
            f"ambarella_asr_{uuid.uuid4().hex[:8]}.wav",
        )
        ten_env.log_info(
            f"ambarella_asr: bin={self.config.bin_path} "
            f"models={self.config.model_dir} "
            f"language={self.config.daemon_language}",
            category=LOG_CATEGORY_KEY_POINT,
        )

    @override
    async def start_connection(self) -> None:
        assert self.config is not None
        if self.daemon is not None:
            await self.stop_connection()
        self.daemon = DaemonClient(
            bin_path=self.config.bin_path,
            flags=self._stub_prefix + self.config.daemon_flags(),
            ready_token=READY_TOKEN,
            logger=self.ten_env,
            load_timeout_s=self.config.load_timeout_s,
            quit_timeout_s=self.config.quit_timeout_s,
            log_category=LOG_CATEGORY_VENDOR,
        )
        # The model load takes tens of seconds; letting it block here would
        # stall POST /start for the whole session.
        self._start_task = asyncio.create_task(self._start_daemon())

    async def _start_daemon(self) -> None:
        assert self.daemon is not None
        try:
            await self.daemon.start()
            self.ten_env.log_info(
                "asr_d is resident and ready",
                category=LOG_CATEGORY_KEY_POINT,
            )
        except DaemonError as err:
            # A load failure cannot be retried into success: the model path is
            # wrong, Cavalry is not loaded, or the VP is out of memory.
            await self._fatal(str(err))

    @override
    def is_connected(self) -> bool:
        return self.daemon is not None and self.daemon.alive

    @override
    async def stop_connection(self) -> None:
        await self._cancel_pending(self._start_task)
        self._start_task = None
        await self._cancel_pending(self._restart_task)
        self._restart_task = None
        if self.daemon is not None:
            # QUIT is the only path that releases VP memory.
            await self.daemon.stop()
            self.daemon = None
        self._buffer.clear()
        if self._wav_path and os.path.exists(self._wav_path):
            os.unlink(self._wav_path)

    @staticmethod
    async def _cancel_pending(task: Optional[asyncio.Task]) -> None:
        """Cancel a background task and wait for it to unwind.

        Guards against self-cancellation: _restart_daemon() calls
        start_connection(), which calls stop_connection() when a daemon is
        already set, and that must not try to cancel and await the very
        task that is currently running it.
        """
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @override
    async def send_audio(
        self, frame: AudioFrame, _session_id: Optional[str]
    ) -> bool:
        if self.daemon is None:
            return False
        buf = frame.lock_buf()
        try:
            new_bytes = bytes(buf)
        finally:
            frame.unlock_buf(buf)
        # Checked before appending, against what this frame would bring the
        # buffer to: appending unconditionally first (and only then
        # checking) can send up to one frame beyond the 30 s ceiling to
        # asr_d. Flushing here keeps the accumulated (already-conforming)
        # audio at or under the limit, and this frame simply starts the
        # next window.
        if len(self._buffer) + len(new_bytes) > MAX_BUFFER_BYTES:
            await self._infer_early()
        self._buffer.extend(new_bytes)
        # A single frame landing exactly on (or, pathologically, past) the
        # ceiling would not have tripped the check above; catch that too.
        if len(self._buffer) >= MAX_BUFFER_BYTES:
            await self._infer_early()
        return True

    async def _infer_early(self) -> None:
        self.ten_env.log_info(
            "30 s window reached; inferring early and still listening"
        )
        # Not the end of a turn, so no finalize_end here.
        await self._infer_buffer()

    @override
    async def finalize(self, _session_id: Optional[str]) -> None:
        try:
            await self._infer_buffer()
        finally:
            # Every path must reach this, or main_control waits forever.
            await self.send_asr_finalize_end()

    async def _infer_buffer(self) -> None:
        # One turn at a time: the shared WAV path and the restart bookkeeping
        # in _on_daemon_error() are only correct if a turn's snapshot, write,
        # inference and error handling all happen atomically against any
        # other turn (send_audio()'s early-inference path included).
        async with self._infer_lock:
            assert self.config is not None
            audio = bytes(self._buffer)
            self._buffer.clear()

            duration_ms = len(audio) * 1000 // BYTES_PER_SECOND
            start_ms = self._elapsed_ms
            self._elapsed_ms += duration_ms

            floor_bytes = self.config.min_audio_ms * BYTES_PER_SECOND // 1000
            if len(audio) < floor_bytes:
                if audio:
                    self.ten_env.log_debug(
                        f"{len(audio)} bytes is below min_audio_ms "
                        f"({self.config.min_audio_ms} ms); not sending to "
                        "the VP"
                    )
                return

            try:
                assert self.daemon is not None
                await self.daemon.wait_ready(self.config.load_timeout_s)
                self._write_wav(audio)
                line = await self.daemon.request(
                    f"INFER {self._wav_path}", self.config.infer_timeout_s
                )
            except DaemonError as err:
                await self._on_daemon_error(err)
                return
            except (OSError, wave.Error) as err:
                await self._non_fatal(
                    f"could not write {self._wav_path}: {err}"
                )
                return

            if line in _SILENCE_LINES:
                self.ten_env.log_debug(
                    f"no speech in {duration_ms} ms of audio"
                )
                return

            if line.startswith("ERR"):
                await self._non_fatal(line)
                return

            if "text=" not in line:
                await self._non_fatal(f"unparseable asr_d reply: {line}")
                return

            text = line.split("text=", 1)[1].strip()
            if not text:
                return

            await self.send_asr_result(
                ASRResult(
                    text=text,
                    final=True,
                    start_ms=start_ms,
                    duration_ms=duration_ms,
                    language=self.config.normalized_language,
                    words=[],
                )
            )

    def _write_wav(self, audio: bytes) -> None:
        # pylint: disable=no-member  # wave.open in 'wb' mode returns Wave_write, not Wave_read
        with wave.open(self._wav_path, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(audio)

    async def _fatal(self, message: str) -> None:
        await self.send_asr_error(
            ModuleError(
                module=MODULE_NAME_ASR,
                code=ModuleErrorCode.FATAL_ERROR.value,
                message=message,
            ),
            ModuleErrorVendorInfo(
                vendor=self.vendor(), code="load", message=message
            ),
        )

    async def _non_fatal(self, message: str) -> None:
        self.ten_env.log_error(
            f"vendor_error: {message}", category=LOG_CATEGORY_VENDOR
        )
        await self.send_asr_error(
            ModuleError(
                module=MODULE_NAME_ASR,
                code=ModuleErrorCode.NON_FATAL_ERROR.value,
                message=message,
            ),
            ModuleErrorVendorInfo(
                vendor=self.vendor(), code="infer", message=message
            ),
        )

    async def _on_daemon_error(self, err: DaemonError) -> None:
        assert self.config is not None
        await self._non_fatal(str(err))
        if self.daemon is not None and self.daemon.alive:
            return
        if self._restart_task is not None and not self._restart_task.done():
            self.ten_env.log_debug(
                "asr_d restart already in progress; not scheduling another"
            )
            return
        if self._restarts >= self.config.restart_max_attempts:
            self.ten_env.log_error(
                "asr_d is dead and the restart cap of "
                f"{self.config.restart_max_attempts} is spent"
            )
            return
        self._restarts += 1
        delay = float(2 ** (self._restarts - 1))
        self.ten_env.log_info(
            f"restarting asr_d, attempt {self._restarts}, after {delay}s"
        )
        # Detached: the turn has already failed and must close out via
        # finalize_end promptly, not wait out the backoff and the reload.
        self._restart_task = asyncio.create_task(self._restart_daemon(delay))

    async def _restart_daemon(self, delay: float) -> None:
        await asyncio.sleep(delay)
        # Serialise the daemon swap against any turn in flight: without the
        # lock, a concurrent _infer_buffer() call could observe self.daemon
        # mid-swap and decide, wrongly, that a second restart is needed.
        async with self._infer_lock:
            await self.start_connection()
