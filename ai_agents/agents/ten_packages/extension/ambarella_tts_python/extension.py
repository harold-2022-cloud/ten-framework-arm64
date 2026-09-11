#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
"""Ambarella TTS extension.

The board's tts_d is a request/response provider, so this rides
AsyncTTS2HttpExtension -- the same base polly_tts uses. Its contract is
transport-agnostic despite the name.
"""

import asyncio

from ten_ai_base.tts2_http import (
    AsyncTTS2HttpClient,
    AsyncTTS2HttpConfig,
    AsyncTTS2HttpExtension,
)
from ten_runtime import AsyncTenEnv

from .ambarella_tts import AmbarellaTTSClient
from .config import AmbarellaTTSConfig
from .const import OUTPUT_SAMPLE_RATE


class AmbarellaTTSExtension(AsyncTTS2HttpExtension):
    """TTS over the on-board tts_d daemon."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: AmbarellaTTSConfig = None
        self.client: AmbarellaTTSClient = None
        self._warm_up_task = None

    async def create_config(self, config_json_str: str) -> AsyncTTS2HttpConfig:
        return AmbarellaTTSConfig.model_validate_json(config_json_str)

    async def create_client(
        self, config: AsyncTTS2HttpConfig, ten_env: AsyncTenEnv
    ) -> AsyncTTS2HttpClient:
        client = AmbarellaTTSClient(config=config, ten_env=ten_env)
        # OpenVoice takes tens of seconds to reach the VP. Warm it in the
        # background so the first spoken sentence does not pay for it, and
        # keep a reference so the task is not garbage-collected mid-load.
        self._warm_up_task = asyncio.create_task(client.warm_up())
        return client

    def vendor(self) -> str:
        return "ambarella"

    def synthesize_audio_sample_rate(self) -> int:
        # tts_d always writes 22050; the client resamples to this before
        # yielding, because the RTSA SDK is told its PCM rate once at init.
        if self.config is None:
            return OUTPUT_SAMPLE_RATE
        return int(self.config.output_sample_rate)
