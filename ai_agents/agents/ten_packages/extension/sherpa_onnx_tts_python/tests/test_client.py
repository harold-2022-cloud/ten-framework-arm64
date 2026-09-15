#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""What the adapter owes the pipeline, and what it owes the engine.

sherpa-onnx synthesises in a C++ call that blocks its thread and hands audio
back through a callback as it goes. Everything here is about keeping those two
facts from leaking: the blocking must stay off the event loop, and the audio
must reach the pipeline while it is still being produced.
"""

import asyncio
import threading

import numpy as np
import pytest
from ten_ai_base.struct import TTS2HttpResponseEventType

from sherpa_onnx_tts_python.config import SherpaOnnxTTSConfig
from sherpa_onnx_tts_python.sherpa_onnx_tts import SherpaOnnxTTSClient

from .fakes import FakeEngine, FakeTenEnv


def make_client(engine, **overrides):
    overrides.setdefault("voice_dir", "/unused")
    config = SherpaOnnxTTSConfig(**overrides)
    return SherpaOnnxTTSClient(
        config=config,
        ten_env=FakeTenEnv(),
        load_engine=lambda _config: engine,
    )


async def drain(client, text="你好", request_id="req-1"):
    return [event async for event in client.get(text, request_id)]


def audio_of(events):
    return b"".join(
        chunk
        for chunk, kind in events
        if kind == TTS2HttpResponseEventType.RESPONSE and chunk
    )


def kinds_of(events):
    return [kind for _, kind in events]


# --- Behaviour 8: empty text ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_empty_text_never_reaches_the_engine(text):
    """main_control sends an empty string to close a turn, every turn.

    Reaching the engine with it would cost a synthesis and, on some voices,
    a phonemiser warning per turn.
    """
    engine = FakeEngine()
    events = await drain(make_client(engine), text=text)

    assert engine.texts == []
    assert kinds_of(events) == [TTS2HttpResponseEventType.END]


# --- Behaviour 4 and 5: text becomes PCM at the declared rate ---------------


@pytest.mark.asyncio
async def test_text_becomes_pcm16_at_the_configured_rate():
    engine = FakeEngine(sample_rate=22050, num_chunks=3, samples_per_chunk=441)
    events = await drain(make_client(engine, output_sample_rate=16000))

    assert engine.texts == ["你好"]
    assert kinds_of(events)[-1] == TTS2HttpResponseEventType.END

    pcm = audio_of(events)
    assert len(pcm) % 2 == 0, "16-bit PCM has an even byte count"
    # 441 samples at 22050 Hz is 20 ms; three of those at 16000 Hz is 960
    # samples, 1920 bytes. Resampling is not sample-exact across chunk
    # boundaries, so this allows a filter's worth of slack.
    assert abs(len(pcm) - 1920) < 200


@pytest.mark.asyncio
async def test_a_voice_already_at_the_target_rate_is_not_converted():
    """Behaviour 5: the rate comes from the model, not from configuration."""
    engine = FakeEngine(sample_rate=16000, num_chunks=2, samples_per_chunk=320)
    events = await drain(make_client(engine, output_sample_rate=16000))

    # 640 samples in, 640 samples out -- exactly, because nothing filtered.
    assert len(audio_of(events)) == 640 * 2


# --- Behaviour 6 and 7: off the event loop, and forwarded as produced -------


@pytest.mark.asyncio
async def test_generate_runs_off_the_event_loop():
    """generate() blocks its thread. Ours has to keep running."""
    engine = FakeEngine(num_chunks=2)
    await drain(make_client(engine))

    assert engine.thread_ids, "the engine was never called"
    assert threading.get_ident() not in engine.thread_ids


@pytest.mark.asyncio
async def test_the_event_loop_keeps_running_during_synthesis():
    engine = FakeEngine(num_chunks=3, chunk_delay=0.05)
    client = make_client(engine)

    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        await drain(client)
    finally:
        ticker.cancel()

    # Three chunks at 50 ms is 150 ms of blocked C++. A loop that was blocked
    # with it would have ticked once or not at all.
    assert ticks > 5


@pytest.mark.asyncio
async def test_audio_is_forwarded_while_the_engine_is_still_producing():
    """Behaviour 7: the first sentence plays before the last is synthesised."""
    engine = FakeEngine(num_chunks=4, chunk_delay=0.05)
    client = make_client(engine)

    still_generating = []
    async for chunk, kind in client.get("你好", "req-1"):
        if kind == TTS2HttpResponseEventType.RESPONSE:
            still_generating.append(engine.generating)

    assert still_generating, "no audio was yielded at all"
    assert still_generating[0] is True, (
        "the first chunk arrived only after generate() returned, so audio is "
        "being collected rather than streamed"
    )


# --- Behaviour 9: cancellation ---------------------------------------------


@pytest.mark.asyncio
async def test_the_callback_returns_non_zero_while_it_should_continue():
    """Zero stops generation. Returning it by mistake truncates every reply."""
    engine = FakeEngine(num_chunks=3)
    await drain(make_client(engine))

    assert engine.returns == [1, 1, 1]
    assert engine.chunks_emitted == 3


@pytest.mark.asyncio
async def test_cancel_stops_the_engine_rather_than_dropping_its_output():
    engine = FakeEngine(num_chunks=10, chunk_delay=0.02)
    client = make_client(engine)

    events = []
    async for event in client.get("你好", "req-1"):
        events.append(event)
        if len(events) == 2:
            await client.cancel()

    assert engine.chunks_emitted < 10, (
        "generate() ran to completion; cancellation discarded audio instead "
        "of stopping the work that produced it"
    )
    assert engine.returns[-1] == 0
    assert kinds_of(events)[-1] == TTS2HttpResponseEventType.FLUSH


@pytest.mark.asyncio
async def test_a_cancelled_request_does_not_cancel_the_next_one():
    engine = FakeEngine(num_chunks=4, chunk_delay=0.02)
    client = make_client(engine)

    stream = client.get("第一句", "req-1")
    async for _ in stream:
        await client.cancel()
        break
    # Close it rather than leaving it to the collector: until it unwinds, the
    # worker thread is still synthesising, and the counter below would be
    # reading two requests at once.
    await stream.aclose()

    engine.chunks_emitted = 0
    events = await drain(client, text="第二句", request_id="req-2")

    assert engine.chunks_emitted == 4
    assert kinds_of(events)[-1] == TTS2HttpResponseEventType.END


# --- Behaviour 10 and 11: lifecycle ----------------------------------------


@pytest.mark.asyncio
async def test_the_model_is_loaded_once_across_requests():
    """Loading costs 2.0 s on the board. Paying it per turn is not an option."""
    loads = []
    engine = FakeEngine(num_chunks=1)

    config = SherpaOnnxTTSConfig(voice_dir="/unused")

    def load(_config):
        loads.append(1)
        return engine

    client = SherpaOnnxTTSClient(
        config=config, ten_env=FakeTenEnv(), load_engine=load
    )

    await drain(client, request_id="req-1")
    await drain(client, request_id="req-2")

    assert len(loads) == 1


@pytest.mark.asyncio
async def test_warm_up_loads_the_model_before_any_request():
    loads = []

    def load(_config):
        loads.append(1)
        return FakeEngine(num_chunks=1)

    client = SherpaOnnxTTSClient(
        config=SherpaOnnxTTSConfig(voice_dir="/unused"),
        ten_env=FakeTenEnv(),
        load_engine=load,
    )
    await client.warm_up()

    assert len(loads) == 1


@pytest.mark.asyncio
async def test_a_voice_that_will_not_load_is_reported_not_raised():
    """warm_up() runs detached; raising there would go nowhere."""

    def load(_config):
        raise RuntimeError("tokens.txt is missing")

    ten_env = FakeTenEnv()
    client = SherpaOnnxTTSClient(
        config=SherpaOnnxTTSConfig(voice_dir="/unused"),
        ten_env=ten_env,
        load_engine=load,
    )
    await client.warm_up()

    assert any("tokens.txt is missing" in line for line in ten_env.lines)

    # The same failure has to reach the caller of get(), which is the path
    # that can actually tell the pipeline about it.
    events = await drain(client)
    assert TTS2HttpResponseEventType.ERROR in kinds_of(events)
    assert kinds_of(events)[-1] == TTS2HttpResponseEventType.END


@pytest.mark.asyncio
async def test_clean_releases_the_model():
    engine = FakeEngine(num_chunks=1)
    client = make_client(engine)

    await drain(client)
    await client.clean()

    assert client.engine is None


@pytest.mark.asyncio
async def test_metadata_reports_the_voice_and_speaker():
    engine = FakeEngine(num_chunks=1)
    client = make_client(engine, voice_dir="/voices/zh", speaker_id=3)

    metadata = client.get_extra_metadata()

    assert metadata["speaker_id"] == 3
    assert "zh" in metadata["voice"]
