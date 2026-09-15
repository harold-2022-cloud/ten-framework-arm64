#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Transcription on the board's CPU.

AsyncASRBaseExtension's "connection" is a loaded model here rather than a
socket, the same way the TTS side's "HTTP client" is a function call into ONNX.
The base class still owns buffering, reconnection and the metrics.
"""

import asyncio
from typing import Optional

from typing_extensions import override

from ten_ai_base.asr import (
    ASRBufferConfig,
    ASRBufferConfigModeKeep,
    ASRResult,
    AsyncASRBaseExtension,
)
from ten_ai_base.message import ModuleError, ModuleErrorCode
from ten_runtime import AsyncTenEnv, AudioFrame

from .config import SherpaOnnxASRConfig
from .const import (
    LOG_CATEGORY_KEY_POINT,
    LOG_CATEGORY_VENDOR,
    MODULE_NAME_ASR,
    SAMPLE_RATE,
)
from .recogniser import SherpaOnnxRecogniser, Transcript

# Thirty seconds of 16 kHz mono PCM16, matching what the daemon sibling keeps.
MAX_BUFFER_BYTES = SAMPLE_RATE * 2 * 30


class SherpaOnnxASRExtension(AsyncASRBaseExtension):
    """Streaming ASR through a local Zipformer."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: Optional[SherpaOnnxASRConfig] = None
        self.recogniser: Optional[SherpaOnnxRecogniser] = None
        self._start_task: Optional[asyncio.Task] = None

    @override
    def vendor(self) -> str:
        return "sherpa_onnx"

    @override
    def input_audio_sample_rate(self) -> int:
        return SAMPLE_RATE

    @override
    def buffer_strategy(self) -> ASRBufferConfig:
        # Catches the frames that arrive while the model is still loading.
        return ASRBufferConfigModeKeep(byte_limit=MAX_BUFFER_BYTES)

    @override
    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)
        config_json, _ = await ten_env.get_property_to_json("")
        try:
            self.config = SherpaOnnxASRConfig.model_validate_json(config_json)
            self.config.validate_model()
        except Exception as err:  # pylint: disable=broad-except
            ten_env.log_error(f"invalid sherpa_onnx_asr config: {err}")
            await self._fatal(str(err))
            return

        ten_env.log_info(
            f"sherpa_onnx_asr: model_dir={self.config.model_dir} "
            f"threads={self.config.num_threads} "
            f"endpointing={self.config.enable_endpoint_detection}",
            category=LOG_CATEGORY_KEY_POINT,
        )

    @override
    async def start_connection(self) -> None:
        if self.config is None:
            await self._fatal("start_connection before a valid config")
            return
        self.recogniser = SherpaOnnxRecogniser(
            config=self.config, ten_env=self.ten_env
        )
        try:
            # Loading takes seconds and blocks a worker thread, not the loop.
            # The base class buffers the audio that arrives meanwhile.
            await self.recogniser.start()
        except Exception as err:  # pylint: disable=broad-except
            self.recogniser = None
            self.ten_env.log_error(
                f"vendor_error: {err}", category=LOG_CATEGORY_VENDOR
            )
            await self._fatal(f"the model failed to load: {err}")

    @override
    def is_connected(self) -> bool:
        return self.recogniser is not None and self.recogniser.is_running()

    @override
    async def stop_connection(self) -> None:
        if self.recogniser is not None:
            await self.recogniser.stop()
            self.recogniser = None

    @override
    async def send_audio(
        self, frame: AudioFrame, session_id: Optional[str]
    ) -> bool:
        if self.recogniser is None:
            return False
        buf = frame.lock_buf()
        try:
            pcm = bytes(buf)
        finally:
            frame.unlock_buf(buf)

        for transcript in await self.recogniser.accept(pcm):
            await self._emit(transcript)
        return True

    @override
    async def finalize(self, _session_id: Optional[str]) -> None:
        if self.recogniser is None:
            return
        for transcript in await self.recogniser.finalize():
            await self._emit(transcript)

    async def _emit(self, transcript: Transcript) -> None:
        assert self.config is not None
        await self.send_asr_result(
            ASRResult(
                text=transcript.text,
                final=transcript.final,
                start_ms=transcript.start_ms,
                duration_ms=transcript.duration_ms,
                language=self.config.language,
                words=[],
            )
        )

    async def _fatal(self, message: str) -> None:
        await self.send_asr_error(
            ModuleError(
                module=MODULE_NAME_ASR,
                code=ModuleErrorCode.FATAL_ERROR.value,
                message=message,
            ),
            None,
        )
