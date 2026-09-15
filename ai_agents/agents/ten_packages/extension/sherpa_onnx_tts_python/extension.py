#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Speech synthesis on the board's CPU.

A request/response provider like polly_tts, so it rides
AsyncTTS2HttpExtension -- the base class's contract is transport-agnostic
despite the name, and here the "transport" is a function call into a loaded
ONNX model.
"""

import asyncio
from typing import Optional

from ten_ai_base.tts2_http import (
    AsyncTTS2HttpClient,
    AsyncTTS2HttpConfig,
    AsyncTTS2HttpExtension,
)
from ten_runtime import AsyncTenEnv

from .config import SherpaOnnxTTSConfig
from .const import OUTPUT_SAMPLE_RATE
from .sherpa_onnx_tts import SherpaOnnxTTSClient


class SherpaOnnxTTSExtension(AsyncTTS2HttpExtension):
    """TTS through a local sherpa-onnx VITS voice."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: Optional[SherpaOnnxTTSConfig] = None
        self.client: Optional[SherpaOnnxTTSClient] = None
        self._warm_up_task: Optional[asyncio.Task] = None

    async def create_config(self, config_json_str: str) -> AsyncTTS2HttpConfig:
        return SherpaOnnxTTSConfig.model_validate_json(config_json_str)

    async def create_client(
        self, config: AsyncTTS2HttpConfig, ten_env: AsyncTenEnv
    ) -> AsyncTTS2HttpClient:
        client = SherpaOnnxTTSClient(config=config, ten_env=ten_env)
        # The voice takes 2.0 s to load on the board. Warm it in the
        # background so the first spoken sentence does not pay for it, and
        # keep a reference so the task is not collected mid-load.
        self._warm_up_task = asyncio.create_task(client.warm_up())
        return client

    async def on_stop(self, ten_env: AsyncTenEnv) -> None:
        # The base class's on_stop() calls client.clean(), which drops the
        # model. Drain the warm-up first: a load that is still running would
        # otherwise finish afterwards and reinstate the reference that
        # clean() just cleared, leaving the model resident for the life of
        # the process.
        await self._cancel_pending(self._warm_up_task)
        self._warm_up_task = None
        await super().on_stop(ten_env)

    @staticmethod
    async def _cancel_pending(task: Optional[asyncio.Task]) -> None:
        """Cancel a background task and wait for it to unwind."""
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def vendor(self) -> str:
        return "sherpa_onnx"

    def synthesize_audio_sample_rate(self) -> int:
        # The voice's own rate is whatever the bundle was trained at; the
        # client converts to this, because the RTSA SDK is told its PCM rate
        # once at initialisation rather than per frame.
        if self.config is None:
            return OUTPUT_SAMPLE_RATE
        return int(self.config.output_sample_rate)
