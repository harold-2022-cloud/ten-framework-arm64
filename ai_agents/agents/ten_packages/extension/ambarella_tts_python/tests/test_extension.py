#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from ambarella_tts_python.ambarella_tts import AmbarellaTTSClient
from ambarella_tts_python.config import AmbarellaTTSConfig
from ambarella_tts_python.extension import AmbarellaTTSExtension

STUB = os.path.join(os.path.dirname(__file__), "stub_daemon.py")

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


@pytest.mark.asyncio
async def test_stop_drains_an_in_flight_warm_up_before_cleaning_the_client():
    """on_stop() must not let client.clean() race the warm-up task's start().

    create_client() detaches client.start() as a background task
    (warm_up()); if on_stop() tore the client down without draining that
    task first, client.clean() -> daemon.stop() would run while the
    warm-up task is still blocked inside daemon.start(), and both touch the
    same DaemonClient._proc. daemon.stop() kills the still-loading process
    either way, and the concurrently running start() then sees that death
    as an EOF on stdout: it raises a DaemonError built from _proc's state
    at that moment -- state stop() may already have started tearing down --
    which warm_up() logs via log_error(). With the fix, on_stop() cancels
    and awaits the warm-up task before the client is ever cleaned, so
    start() unwinds via a clean CancelledError and log_error is never
    called for this at all.

    The "no_ready" stub scenario never emits a readiness line, so the
    warm-up task is guaranteed to still be in flight when on_stop() runs --
    no timing luck required to reach the race window itself.
    """
    extension = AmbarellaTTSExtension("test")
    env = AsyncMock()
    env.log_info = MagicMock()
    env.log_debug = MagicMock()
    env.log_error = MagicMock()
    env.log_warn = MagicMock()
    # AsyncTTS2BaseExtension.on_init() sets self.ten_env; the base class's
    # on_stop() (send_usage_metrics(), in particular) reads self.ten_env,
    # not the ten_env passed into on_stop() -- so it must be set the same
    # way here, without running the rest of on_init().
    extension.ten_env = env

    config = AmbarellaTTSConfig(
        bin_path=sys.executable,
        model_dir="/models/openvoice",
        quit_timeout_s=0.05,
        params={"scenario": "no_ready", "ready-token": "READY tts"},
    )
    extension.config = config
    extension.client = AmbarellaTTSClient(config=config, ten_env=env)
    extension.client._stub_prefix = [STUB]
    extension._warm_up_task = asyncio.create_task(extension.client.warm_up())
    # Let the subprocess actually spawn before tearing down; "no_ready"
    # then guarantees it stays mid-load indefinitely on its own.
    await asyncio.sleep(0.05)
    assert not extension._warm_up_task.done()

    # Catches both an unretrieved task exception and a task destroyed
    # while still pending -- asyncio routes both through this handler.
    loop = asyncio.get_running_loop()
    unhandled = []
    loop.set_exception_handler(lambda _loop, ctx: unhandled.append(ctx))
    try:
        await extension.on_stop(env)
    finally:
        loop.set_exception_handler(None)

    assert extension._warm_up_task is None
    assert unhandled == []
    assert env.log_error.call_count == 0
