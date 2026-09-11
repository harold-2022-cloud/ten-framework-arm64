#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import asyncio
import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from ten_ai_base.asr import ASRBufferConfigModeKeep
from ten_ai_base.message import ModuleErrorCode

from ambarella_asr_python.const import BYTES_PER_SECOND, MAX_BUFFER_BYTES
from ambarella_asr_python.daemon import DaemonError
from ambarella_asr_python.extension import AmbarellaASRExtension

STUB = os.path.join(os.path.dirname(__file__), "stub_daemon.py")


def make_env(scenario="ok", **overrides):
    config = {
        "bin_path": sys.executable,
        "model_dir": "/models/whisper",
        "params": {"scenario": scenario, "language": "english"},
    }
    config.update(overrides)
    env = AsyncMock()
    env.log_info = MagicMock()
    env.log_error = MagicMock()
    env.log_debug = MagicMock()
    env.log_warn = MagicMock()
    env.get_property_to_json = AsyncMock(
        return_value=(json.dumps(config), None)
    )
    # AsyncASRBaseExtension.on_init() (ten_ai_base 0.7) reads "auto_connect"
    # and unpacks the result; an unstubbed AsyncMock attribute iterates as
    # empty, so the unpack itself raises before our on_init ever runs. This
    # extension always calls start_connection() explicitly and never reaches
    # on_start() in these tests, so the value has no effect on behaviour here.
    env.get_property_bool = AsyncMock(return_value=(True, None))
    return env


async def make_started(scenario="ok", **overrides):
    """Build an extension whose daemon is the stub, already ready."""
    extension = AmbarellaASRExtension("test_ambarella_asr")
    env = make_env(scenario, **overrides)
    extension.ten_env = env
    extension.send_asr_result = AsyncMock()
    extension.send_asr_error = AsyncMock()
    extension.send_asr_finalize_end = AsyncMock()
    await extension.on_init(env)
    # The stub is a script, so the interpreter is the binary and the script
    # path is the first flag.
    extension._stub_prefix = [STUB]
    await extension.start_connection()
    await extension.daemon.wait_ready(10.0)
    return extension


def speech(ms):
    return b"\x01\x00" * int(BYTES_PER_SECOND * ms / 1000 / 2)


def test_fixed_contract():
    extension = AmbarellaASRExtension("test")
    assert extension.vendor() == "ambarella"
    assert extension.input_audio_sample_rate() == 16000
    assert isinstance(extension.buffer_strategy(), ASRBufferConfigModeKeep)


@pytest.mark.asyncio
async def test_happy_path_emits_a_final_result():
    extension = await make_started()
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        assert extension.send_asr_result.await_count == 1
        result = extension.send_asr_result.await_args[0][0]
        assert result.text == "hello world"
        assert result.final is True
        assert result.language == "en-US"
        assert result.duration_ms == 1000
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_no_speech_is_not_an_error():
    extension = await make_started("no_speech")
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        assert extension.send_asr_result.await_count == 0
        assert extension.send_asr_error.await_count == 0
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_vendor_error_reports_and_still_finalizes():
    extension = await make_started("err_infer")
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        assert extension.send_asr_error.await_count == 1
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_timeout_reports_and_still_finalizes():
    extension = await make_started("hang_on_infer", infer_timeout_s=0.5)
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        assert extension.send_asr_error.await_count == 1
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_crash_reports_and_still_finalizes():
    extension = await make_started("die_on_infer", restart_max_attempts=0)
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        assert extension.send_asr_error.await_count == 1
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_audio_below_the_floor_never_reaches_the_vp():
    extension = await make_started()
    try:
        extension._buffer.extend(speech(50))
        await extension.finalize(None)
        assert extension.send_asr_result.await_count == 0
        assert extension.send_asr_error.await_count == 0
        # The invariant holds even on the cheapest path.
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_empty_buffer_still_finalizes():
    extension = await make_started()
    try:
        await extension.finalize(None)
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_thirty_second_ceiling_infers_early_and_keeps_listening():
    extension = await make_started()
    try:
        frame = MagicMock()
        frame.lock_buf = MagicMock(return_value=bytearray(MAX_BUFFER_BYTES))
        frame.unlock_buf = MagicMock()
        assert await extension.send_audio(frame, None) is True
        assert extension.send_asr_result.await_count == 1
        # An early inference is not the end of a turn.
        assert extension.send_asr_finalize_end.await_count == 0
        assert len(extension._buffer) == 0
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_thirty_second_ceiling_never_overshoots():
    """A frame that would push the buffer past the ceiling must not be
    included in the inference it triggers.

    Checking the limit *after* appending (the bug) lets the frame that
    crosses the ceiling ride along into that same INFER, sending up to one
    frame beyond 960 000 bytes. Checking before appending instead must
    infer on only what was already buffered, and carry this frame over to
    start the next window rather than dropping it.
    """
    extension = await make_started()
    try:
        write_calls = []
        original_write_wav = extension._write_wav

        def spy_write_wav(audio):
            write_calls.append(audio)
            original_write_wav(audio)

        extension._write_wav = spy_write_wav

        under_by = 100
        frame1 = MagicMock()
        frame1.lock_buf = MagicMock(
            return_value=bytearray(MAX_BUFFER_BYTES - under_by)
        )
        frame1.unlock_buf = MagicMock()
        assert await extension.send_audio(frame1, None) is True
        # Still under the ceiling: no inference yet.
        assert len(write_calls) == 0
        assert len(extension._buffer) == MAX_BUFFER_BYTES - under_by

        overshoot_by = 200
        frame2 = MagicMock()
        frame2.lock_buf = MagicMock(return_value=bytearray(overshoot_by))
        frame2.unlock_buf = MagicMock()
        assert await extension.send_audio(frame2, None) is True

        assert len(write_calls) == 1
        assert len(write_calls[0]) == MAX_BUFFER_BYTES - under_by
        assert len(write_calls[0]) <= MAX_BUFFER_BYTES
        # frame2 was not dropped: it starts the next window.
        assert len(extension._buffer) == overshoot_by
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_send_audio_accumulates_without_inferring():
    extension = await make_started()
    try:
        frame = MagicMock()
        frame.lock_buf = MagicMock(return_value=bytearray(speech(500)))
        frame.unlock_buf = MagicMock()
        await extension.send_audio(frame, None)
        assert len(extension._buffer) == len(speech(500))
        assert extension.send_asr_result.await_count == 0
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_start_times_are_monotonic_across_turns():
    extension = await make_started()
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        extension._buffer.extend(speech(2000))
        await extension.finalize(None)
        first, second = [
            call[0][0] for call in extension.send_asr_result.await_args_list
        ]
        assert first.start_ms == 0
        assert second.start_ms == 1000
        assert second.duration_ms == 2000
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_load_failure_is_fatal():
    extension = AmbarellaASRExtension("test")
    env = make_env("die_on_load")
    extension.ten_env = env
    extension.send_asr_error = AsyncMock()
    await extension.on_init(env)
    extension._stub_prefix = [STUB]
    await extension.start_connection()
    await extension._start_task
    assert extension.send_asr_error.await_count == 1
    error = extension.send_asr_error.await_args[0][0]
    # ModuleErrorCode is a (str, Enum); ModuleError.code is int, so pydantic
    # coerces the numeric string to int on construction. Match the repo's
    # established idiom for this (see xai_tts_python's tests) rather than
    # comparing str to int directly.
    assert error.code == int(ModuleErrorCode.FATAL_ERROR.value)
    await extension.stop_connection()


@pytest.mark.asyncio
async def test_missing_bin_path_is_fatal_at_init():
    extension = AmbarellaASRExtension("test")
    env = make_env(bin_path="")
    extension.ten_env = env
    extension.send_asr_error = AsyncMock()
    await extension.on_init(env)
    assert extension.send_asr_error.await_count == 1


@pytest.mark.asyncio
async def test_stop_removes_the_temp_wav():
    extension = await make_started()
    extension._buffer.extend(speech(1000))
    await extension.finalize(None)
    path = extension._wav_path
    assert os.path.exists(path)
    await extension.stop_connection()
    assert not os.path.exists(path)


# --- Concurrency: send_audio() (the audio_frame consumer task) and
# finalize() (the on_data task) run on separate asyncio tasks in the real
# base class, and are free to interleave at any await point. ---


@pytest.mark.asyncio
async def test_concurrent_turns_do_not_interleave_the_shared_wav():
    """Two turns overlapping in time must not race on the reused WAV path.

    Without _infer_lock, the second turn's _write_wav() runs as soon as the
    first turn's daemon.request() yields control -- long before the first
    reply (deliberately slowed here) comes back -- overwriting the file the
    daemon may still be reading for the first INFER. Serialised, the second
    write cannot happen until the first turn's whole request/response cycle
    has completed.
    """
    extension = await make_started(
        params={
            "scenario": "ok",
            "language": "english",
            "infer_delay": 0.3,
        }
    )
    try:
        write_times = []
        original_write_wav = extension._write_wav

        def spy_write_wav(audio):
            write_times.append(time.monotonic())
            original_write_wav(audio)

        extension._write_wav = spy_write_wav

        async def turn(payload):
            extension._buffer.extend(payload)
            await extension._infer_buffer()

        await asyncio.gather(turn(speech(1000)), turn(speech(1000)))

        assert len(write_times) == 2
        # The second write must not land inside the first turn's in-flight
        # request window (delayed 0.3s by the stub); a wide margin below
        # that keeps this robust to scheduling jitter while still failing
        # outright (typically a few ms apart) without the lock.
        assert write_times[1] - write_times[0] >= 0.2
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_concurrent_dead_daemon_turns_schedule_only_one_restart():
    """Two turns that both find the daemon dead must not both restart it.

    Without the in-flight-restart guard in _on_daemon_error(), a second
    turn that also finds the daemon dead reads the same stale
    self._restarts value the first turn already acted on and schedules a
    second restart -- which would tear down the daemon the first restart
    is still loading.

    Death is forced directly (daemon.alive = False) rather than driven
    through a real crashing stub. A first version of this test drove it
    through die_on_infer and was flaky: DaemonClient.alive races the OS
    reaping the child against EOF detection on stdout, so immediately
    after a real crash, self.daemon.alive is not deterministically False
    -- when the reap had not yet landed, *both* calls below would return
    at the alive check before ever reaching the guard this test exists to
    pin, leaving self._restarts at 0 instead of 1. Calling
    _on_daemon_error() directly, twice, against a daemon fixed as dead
    exercises the exact same guard deterministically: the first call must
    set self._restart_task, and the second call must see it in flight and
    decline to schedule another, rather than the test merely asserting
    state it set up itself.
    """
    extension = await make_started()
    try:
        extension.daemon = AsyncMock()
        extension.daemon.alive = False

        await extension._on_daemon_error(DaemonError("boom 1"))
        first_restart_task = extension._restart_task
        assert extension._restarts == 1
        assert first_restart_task is not None

        # A second turn hitting the same still-dead daemon must see the
        # first restart as already in flight and not schedule another.
        await extension._on_daemon_error(DaemonError("boom 2"))

        assert extension._restarts == 1
        assert extension._restart_task is first_restart_task
        assert extension.send_asr_error.await_count == 2
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_daemon_error_handling_returns_promptly_without_waiting_for_the_restart_backoff():
    """_on_daemon_error() must not block the turn for the restart backoff.

    Fixes the daemon as unambiguously dead and drives _on_daemon_error()
    directly, for the same reason
    test_concurrent_dead_daemon_turns_schedule_only_one_restart does (real
    death detection races the OS reaping the child against reading EOF on
    stdout, so daemon.alive is not deterministic immediately after a real
    crash). This test pins exactly one thing: scheduling a restart must not
    make the caller (finalize(), via _infer_buffer()) wait out the backoff
    (>= 1s by default) before it can return and reach finalize_end.
    """
    extension = await make_started()
    try:
        extension.daemon = AsyncMock()
        extension.daemon.alive = False

        start = time.monotonic()
        await extension._on_daemon_error(DaemonError("boom"))
        elapsed = time.monotonic() - start

        assert elapsed < 0.5
        assert extension._restarts == 1
        assert extension._restart_task is not None
        assert extension.send_asr_error.await_count == 1
    finally:
        await extension.stop_connection()
