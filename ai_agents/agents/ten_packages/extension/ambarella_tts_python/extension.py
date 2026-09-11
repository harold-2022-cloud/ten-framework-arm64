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
from typing import Optional

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
        self._warm_up_task: Optional[asyncio.Task] = None

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

    async def on_stop(self, ten_env: AsyncTenEnv) -> None:
        # The base class's on_stop() calls client.clean(), which stops the
        # daemon. That is not safe to run concurrently with the warm-up
        # task's still-in-flight client.start(): both read and write the
        # same DaemonClient._proc, and a stop() that lands mid-load can
        # race start()'s reader into observing a torn-down process instead
        # of the clean DaemonError it is written to handle. Drain the
        # warm-up task first, the same way the ASR sibling drains its
        # start task in stop_connection() before calling daemon.stop().
        await self._cancel_pending(self._warm_up_task)
        self._warm_up_task = None
        await super().on_stop(ten_env)

    @staticmethod
    async def _cancel_pending(task: Optional[asyncio.Task]) -> None:
        """Cancel a background task and wait for it to unwind.

        A no-op for a task that is already done (the common case: warm-up
        finished, one way or another, long before on_stop() runs).
        """
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def vendor(self) -> str:
        return "ambarella"

    def synthesize_audio_sample_rate(self) -> int:
        # tts_d always writes 22050; the client resamples to this before
        # yielding, because the RTSA SDK is told its PCM rate once at init.
        if self.config is None:
            return OUTPUT_SAMPLE_RATE
        return int(self.config.output_sample_rate)
