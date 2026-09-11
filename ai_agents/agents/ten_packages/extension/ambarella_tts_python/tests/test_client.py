#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from ten_ai_base.tts2_http import TTS2HttpResponseEventType

from ambarella_tts_python.ambarella_tts import (
    AmbarellaTTSClient,
    sanitise,
    split_text,
)
from ambarella_tts_python.config import AmbarellaTTSConfig
from ambarella_tts_python.const import OUTPUT_SAMPLE_RATE
from ambarella_tts_python.daemon import DaemonError

STUB = os.path.join(os.path.dirname(__file__), "stub_daemon.py")


def make_client(scenario="ok", rate=22050, **overrides):
    fields = {
        "bin_path": sys.executable,
        "model_dir": "/models/openvoice",
        "params": {
            "scenario": scenario,
            "stub-rate": rate,
            "speaker_id": 0,
            # The stub's own default ready token is "READY asr" (it doubles
            # as the ASR package's stub too); without this override it never
            # emits READY_TOKEN ("READY tts") and start() hangs until
            # load_timeout_s. It also gates the stub's WAV-writing branch.
            "ready-token": "READY tts",
        },
    }
    fields.update(overrides)
    config = AmbarellaTTSConfig(**fields)
    env = AsyncMock()
    env.log_info = MagicMock()
    env.log_error = MagicMock()
    env.log_debug = MagicMock()
    env.log_warn = MagicMock()
    client = AmbarellaTTSClient(config=config, ten_env=env)
    client._stub_prefix = [STUB]
    return client


async def drain(client, text="hello there", request_id="r1"):
    chunks = []
    events = []
    async for payload, event in client.get(text, request_id):
        chunks.append(payload)
        events.append(event)
    return chunks, events


def test_sanitise_folds_newlines_that_would_break_the_protocol():
    assert sanitise("one\ntwo") == "one two"
    assert sanitise("one\r\ntwo\tthree") == "one two three"
    assert sanitise("  spaced   out  ") == "spaced out"
    assert sanitise("\n\n\t ") == ""


def test_sanitise_keeps_inner_spaces():
    assert sanitise("hello world") == "hello world"


def test_split_text_returns_one_piece_when_short():
    assert split_text("hello world", 200) == ["hello world"]


def test_split_text_splits_a_long_sentence_on_punctuation():
    text = "a" * 120 + ", " + "b" * 120 + ". " + "c" * 20
    pieces = split_text(text, 200)
    assert len(pieces) >= 2
    assert "".join(p.replace(" ", "") for p in pieces) == text.replace(" ", "")


def test_split_text_hard_splits_when_there_is_no_punctuation():
    text = "x" * 500
    pieces = split_text(text, 200)
    assert all(len(p) <= 200 for p in pieces)
    assert "".join(pieces) == text


@pytest.mark.asyncio
async def test_empty_text_ends_without_touching_the_daemon():
    client = make_client()
    try:
        chunks, events = await drain(client, "   \n  ")
        assert events == [TTS2HttpResponseEventType.END]
        assert chunks == [None]
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_audio_is_resampled_to_sixteen_kilohertz():
    client = make_client(rate=22050)
    try:
        chunks, events = await drain(client)
        assert events[-1] == TTS2HttpResponseEventType.END
        audio = b"".join(c for c in chunks if c)
        # The stub synthesises 0.2 s; at 16 kHz PCM16 that is 6400 bytes.
        # Allow a few samples of filter delay either way.
        assert abs(len(audio) - 6400) < 400
        assert len(audio) % 2 == 0
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_native_rate_is_passed_through_when_it_already_matches():
    client = make_client(rate=16000)
    try:
        chunks, _events = await drain(client)
        audio = b"".join(c for c in chunks if c)
        assert len(audio) == 6400
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_a_header_rate_other_than_native_is_logged_loudly():
    client = make_client(rate=24000)
    try:
        await drain(client)
        messages = [
            call[0][0] for call in client.ten_env.log_error.call_args_list
        ]
        assert any("24000" in m for m in messages)
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_chunks_are_twenty_milliseconds():
    client = make_client()
    try:
        chunks, _events = await drain(client)
        audio_chunks = [c for c in chunks if c]
        assert all(len(c) <= 640 for c in audio_chunks)
        assert len(audio_chunks[0]) == 640
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_cancel_flushes_mid_stream_without_killing_the_daemon():
    # get() clears _is_cancelled on entry, so a cancel issued before get() is
    # started can never be observed -- this drives the stream manually and
    # cancels only once real audio is flowing, the way a barge-in would.
    client = make_client()
    try:
        chunks = []
        events = []
        async for payload, event in client.get("hello there", "r1"):
            chunks.append(payload)
            events.append(event)
            if event == TTS2HttpResponseEventType.RESPONSE:
                await client.cancel()
        assert events[-1] == TTS2HttpResponseEventType.FLUSH
        assert chunks[-1] is None
        assert client._daemon.alive is True
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_vendor_error_yields_an_error_event():
    client = make_client("err_infer")
    try:
        _chunks, events = await drain(client)
        assert TTS2HttpResponseEventType.ERROR in events
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_synthesis_output_rate_is_declared_as_sixteen_k():
    client = make_client()
    assert client.config.output_sample_rate == OUTPUT_SAMPLE_RATE
    await client.clean()


@pytest.mark.asyncio
async def test_metadata_reports_the_voice():
    client = make_client()
    client.config.params["speaker_id"] = 3
    assert client.get_extra_metadata()["speaker_id"] == 3
    await client.clean()


@pytest.mark.asyncio
async def test_respawns_are_capped():
    client = make_client(restart_max_attempts=1)
    try:
        # Two spawns are allowed, the third is refused rather than paying a
        # third model load.
        await client.start()
        await client._daemon.stop()
        await client.start()
        await client._daemon.stop()
        with pytest.raises(DaemonError, match="restart cap"):
            await client.start()
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_warm_up_swallows_a_load_failure():
    client = make_client("die_on_load")
    try:
        await client.warm_up()
        messages = [
            call[0][0] for call in client.ten_env.log_error.call_args_list
        ]
        assert any("warm up" in m for m in messages)
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_nonexistent_bin_path_surfaces_as_an_error_event():
    # bin_path is operator-supplied in property.json, so a wrong or
    # non-executable path is the likeliest production misconfiguration: it
    # must come back as an ERROR event naming the path, not an unhandled
    # exception or a hang.
    bad_path = os.path.join(os.path.dirname(__file__), "no_such_tts_d_binary")
    client = make_client(bin_path=bad_path)
    try:
        chunks, events = await drain(client)
        assert TTS2HttpResponseEventType.ERROR in events
        messages = [c.decode("utf-8") for c in chunks if c]
        assert any(bad_path in m for m in messages)
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_clean_removes_the_temp_wav():
    client = make_client()
    await drain(client)
    path = client._wav_path
    assert os.path.exists(path)
    await client.clean()
    assert not os.path.exists(path)
