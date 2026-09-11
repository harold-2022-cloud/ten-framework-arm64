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
async def test_request_timeout_kills_the_wedged_daemon():
    """A timed-out request must not leave the daemon alive-but-hung.

    Without killing on timeout, the process is still running (just never
    going to answer this INFER): `alive` stays True, so the caller's death
    -based restart path never fires, and the *next* turn's request() would
    read this turn's still-pending reply instead of its own -- worse here,
    since it would go on to read a WAV path tts_d may still be writing.
    """
    client = make_client("hang_on_infer")
    try:
        await client.start()
        with pytest.raises(DaemonError, match="did not answer"):
            await client.request("INFER hello /tmp/stub_hang.wav", 0.5)
        assert client.alive is False
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_broken_pipe_on_write_raises_daemon_error():
    """A write to a dead daemon's stdin must surface as a DaemonError.

    BrokenPipeError/ConnectionResetError are OSError, so an uncaught one
    would be indistinguishable, at the call site, from a real WAV-read
    failure -- and would bypass the daemon-death restart path entirely.
    The daemon is not actually dead here; the write is forced to fail so
    the exact failure mode is reproduced deterministically rather than
    raced against real process-death timing.
    """
    client = make_client()
    try:
        await client.start()

        def _boom(_data):
            raise BrokenPipeError("write failed")

        client._proc.stdin.write = _boom
        with pytest.raises(DaemonError, match=re.escape(sys.executable)):
            await client.request("INFER hello /tmp/stub_out.wav", 5.0)
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_connection_reset_on_drain_raises_daemon_error():
    client = make_client()
    try:
        await client.start()

        async def _boom():
            raise ConnectionResetError("connection reset")

        client._proc.stdin.drain = _boom
        with pytest.raises(DaemonError, match=re.escape(sys.executable)):
            await client.request("INFER hello /tmp/stub_out.wav", 5.0)
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_stop_does_not_race_an_in_flight_request():
    """Teardown must not interleave QUIT with an in-flight INFER.

    The daemon is a single sequential reader: whichever command actually
    lands in its stdin pipe first is the one it answers first. Creating
    stop_task before req_task (neither awaited yet) lets stop()'s QUIT
    reach the pipe before request() ever writes -- a session tearing down
    while a request is (about to be) in flight. Without _lock guarding
    stop() too, the daemon answers QUIT ("OK bye") and exits without ever
    reading the buffered INFER line, and request()'s reader has no way to
    tell that terminal "OK" apart from a real reply -- it would return
    "OK bye" to whatever turn issued the INFER.

    Guarded, stop() and request() serialise on the same lock: stop() reaps
    the process before request() ever gets to write, so request() sees a
    clean "not running" DaemonError instead of a corrupted reply.
    """
    client = make_client()
    try:
        await client.start()
        stop_task = asyncio.create_task(client.stop())
        req_task = asyncio.create_task(
            client.request("INFER hello /tmp/stub_out.wav", 5.0)
        )
        await stop_task
        with pytest.raises(DaemonError, match="not running"):
            await req_task
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
        requested = ["/tmp/stub_a.wav", "/tmp/stub_b.wav", "/tmp/stub_c.wav"]
        lines = await asyncio.gather(
            client.request(f"INFER a {requested[0]}", 10.0),
            client.request(f"INFER b {requested[1]}", 10.0),
            client.request(f"INFER c {requested[2]}", 10.0),
        )
        # Unlike the ASR stub's fixed reply, the TTS stub echoes the
        # requested path in "OK wav=...", so this can check that each
        # reply is paired with its own request -- not merely that all
        # three replies are individually well-formed.
        returned = [line.split("wav=", 1)[1].split(" ", 1)[0] for line in lines]
        assert returned == requested
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
