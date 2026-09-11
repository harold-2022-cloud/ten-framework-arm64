#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import asyncio
import os
import re
import sys
import time
from unittest.mock import MagicMock

import pytest

from ambarella_tts_python.daemon import DaemonClient, DaemonError

STUB = os.path.join(os.path.dirname(__file__), "stub_daemon.py")


def make_client(scenario="ok", **kwargs):
    logger = MagicMock()
    return DaemonClient(
        bin_path=sys.executable,
        # stub_daemon.py defaults to --ready-token "READY asr"; it must be
        # told to speak the TTS dialect (readiness token + wav-write
        # replies) or the client hangs waiting for a token that never comes.
        flags=[STUB, "--scenario", scenario, "--ready-token", "READY tts"],
        ready_token="READY tts",
        logger=logger,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_start_returns_the_ready_line():
    client = make_client()
    try:
        assert await client.start() == "READY tts"
        assert client.alive is True
        assert client.ready is True
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_request_returns_the_ok_line():
    client = make_client()
    try:
        await client.start()
        line = await client.request("INFER hello /tmp/stub_out.wav", 10.0)
        assert line.startswith("OK wav=/tmp/stub_out.wav frames=")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_err_is_returned_not_raised():
    client = make_client("no_speech")
    try:
        await client.start()
        line = await client.request("INFER hello /tmp/stub_no_speech.wav", 10.0)
        assert line == "ERR no speech."
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_vendor_noise_is_skipped_not_parsed():
    client = make_client("noise")
    try:
        await client.start()
        line = await client.request("INFER hello /tmp/stub_noise.wav", 10.0)
        assert line.startswith("OK wav=")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_noise_is_logged_under_the_vendor_category():
    client = make_client("noise")
    try:
        await client.start()
        assert client._log.log_debug.called
        _args, kwargs = client._log.log_debug.call_args
        assert kwargs["category"] == "vendor"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_missing_ready_times_out_with_a_daemon_error():
    client = make_client("no_ready", load_timeout_s=0.5)
    with pytest.raises(DaemonError, match="did not print"):
        await client.start()
    assert client.alive is False


@pytest.mark.asyncio
async def test_err_during_load_raises():
    client = make_client("die_on_load", load_timeout_s=5.0)
    with pytest.raises(DaemonError, match="ERR init"):
        await client.start()


@pytest.mark.asyncio
async def test_death_mid_request_raises():
    client = make_client("die_on_infer")
    try:
        await client.start()
        with pytest.raises(DaemonError, match="exited"):
            await client.request("INFER hello /tmp/stub_die.wav", 10.0)
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_request_timeout_raises():
    client = make_client("hang_on_infer")
    try:
        await client.start()
        with pytest.raises(DaemonError, match="did not answer"):
            await client.request("INFER hello /tmp/stub_hang.wav", 0.5)
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_stop_sends_quit_and_reaps_the_process():
    client = make_client()
    await client.start()
    await client.stop()
    assert client.alive is False
    assert client.ready is False


@pytest.mark.asyncio
async def test_requests_are_serialised_under_the_lock():
    client = make_client()
    try:
        await client.start()
        lines = await asyncio.gather(
            client.request("INFER a /tmp/stub_a.wav", 10.0),
            client.request("INFER b /tmp/stub_b.wav", 10.0),
            client.request("INFER c /tmp/stub_c.wav", 10.0),
        )
        assert all(line.startswith("OK wav=") for line in lines)
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_wait_ready_reraises_a_background_start_failure():
    client = make_client("die_on_load", load_timeout_s=5.0)
    task = asyncio.create_task(client.start())
    await asyncio.sleep(0.5)
    with pytest.raises(DaemonError):
        await client.wait_ready(1.0)
    task.cancel()


@pytest.mark.asyncio
async def test_request_before_start_raises():
    client = make_client()
    with pytest.raises(DaemonError, match="not running"):
        await client.request("INFER hello /tmp/x.wav", 1.0)


@pytest.mark.asyncio
async def test_start_with_bad_bin_path_raises_the_real_cause():
    """A missing/unexecutable binary must surface as a DaemonError naming
    the path immediately, not as a raw OSError, and not as a generic
    "was not ready" message discovered only after a full timeout."""
    bad_path = os.path.join(os.path.dirname(__file__), "no-such-daemon-binary")
    client = DaemonClient(
        bin_path=bad_path,
        flags=[],
        ready_token="READY tts",
        logger=MagicMock(),
    )
    with pytest.raises(DaemonError, match=re.escape(bad_path)):
        await client.start()
    assert client.alive is False

    began = time.monotonic()
    with pytest.raises(DaemonError, match=re.escape(bad_path)):
        await client.wait_ready(30.0)
    assert time.monotonic() - began < 1.0


@pytest.mark.asyncio
async def test_wait_ready_wakes_promptly_when_start_fails_while_blocked():
    """The real race: a waiter already blocked in wait_ready() must be
    woken the moment start() fails, not left to sleep through its own
    (generous) timeout."""
    client = make_client("die_on_load", load_timeout_s=5.0)
    waiter = asyncio.create_task(client.wait_ready(30.0))
    await asyncio.sleep(0)  # let the waiter block on the ready event
    start_task = asyncio.create_task(client.start())
    try:
        began = time.monotonic()
        with pytest.raises(DaemonError, match="ERR init"):
            await waiter
        assert time.monotonic() - began < 10.0

        with pytest.raises(DaemonError, match="ERR init"):
            await start_task
    finally:
        await client.stop()
