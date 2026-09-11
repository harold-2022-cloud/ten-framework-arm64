#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ambarella_tts_python.ambarella_tts import AmbarellaTTSClient
from ambarella_tts_python.config import AmbarellaTTSConfig
from ambarella_tts_python.extension import AmbarellaTTSExtension

CONFIG = json.dumps(
    {
        "bin_path": "/opt/tts_d",
        "model_dir": "/models/openvoice",
        "params": {"speaker_id": 0},
    }
)


def test_vendor_name():
    assert AmbarellaTTSExtension("test").vendor() == "ambarella"


@pytest.mark.asyncio
async def test_create_config_parses_the_property_json():
    extension = AmbarellaTTSExtension("test")
    config = await extension.create_config(CONFIG)
    assert isinstance(config, AmbarellaTTSConfig)
    assert config.bin_path == "/opt/tts_d"
    assert config.model_dir == "/models/openvoice"


@pytest.mark.asyncio
async def test_create_client_returns_the_ambarella_client():
    extension = AmbarellaTTSExtension("test")
    config = await extension.create_config(CONFIG)
    env = AsyncMock()
    env.log_info = MagicMock()
    env.log_debug = MagicMock()
    # log_error is on the path the spawn failure below takes; left as an
    # AsyncMock it would return an unawaited coroutine once that path is
    # actually let to run (see the finally block).
    env.log_error = MagicMock()
    client = await extension.create_client(config, env)
    try:
        assert isinstance(client, AmbarellaTTSClient)
    finally:
        # create_client() detaches warm_up() via asyncio.create_task() to
        # spawn the daemon in the background; it targets a nonexistent
        # bin_path here, and the spawn failure is caught inside warm_up()
        # itself, so it never escapes -- but the task must still be let to
        # finish (or cancelled) before the test ends, or pytest reports
        # "Task was destroyed but it is pending!".
        await extension._warm_up_task


@pytest.mark.asyncio
async def test_declared_rate_is_sixteen_k():
    extension = AmbarellaTTSExtension("test")
    extension.config = await extension.create_config(CONFIG)
    assert extension.synthesize_audio_sample_rate() == 16000


@pytest.mark.asyncio
async def test_declared_rate_follows_the_property():
    extension = AmbarellaTTSExtension("test")
    extension.config = await extension.create_config(
        json.dumps(
            {
                "bin_path": "/b",
                "model_dir": "/m",
                "output_sample_rate": 22050,
            }
        )
    )
    assert extension.synthesize_audio_sample_rate() == 22050
