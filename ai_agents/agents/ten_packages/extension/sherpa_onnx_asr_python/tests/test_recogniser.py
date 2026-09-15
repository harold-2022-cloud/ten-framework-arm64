#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""What the adapter owes the pipeline while driving a streaming recogniser.

The engine decodes in a blocking C++ call and reports partial text after every
step. Everything here is about what reaches the graph: partials that changed,
one final per utterance, and an event loop that keeps running.
"""

import asyncio
import threading

import pytest

from sherpa_onnx_asr_python.config import SherpaOnnxASRConfig
from sherpa_onnx_asr_python.recogniser import (
    SherpaOnnxRecogniser,
    pcm16_to_float32,
)

from .fakes import FakeRecogniser, FakeTenEnv


def make(engine, **overrides):
    overrides.setdefault("model_dir", "/unused")
    return SherpaOnnxRecogniser(
        config=SherpaOnnxASRConfig(**overrides),
        ten_env=FakeTenEnv(),
        load_engine=lambda _config: engine,
    )


SILENCE = b"\x00\x00" * 320  # 20 ms at 16 kHz


async def feed(recogniser, frames=1, payload=SILENCE):
    out = []
    for _ in range(frames):
        out.extend(await recogniser.accept(payload))
    return out


# --- Behaviour 4: PCM conversion ------------------------------------------


def test_pcm16_becomes_float32_in_unit_range():
    import numpy as np

    pcm = np.array([0, 32767, -32768, 16384], dtype=np.int16).tobytes()
    samples = pcm16_to_float32(pcm)

    assert samples.dtype == np.float32
    assert samples[0] == pytest.approx(0.0)
    assert samples[1] == pytest.approx(1.0, abs=1e-4)
    assert samples[2] == pytest.approx(-1.0, abs=1e-4)
    assert samples[3] == pytest.approx(0.5, abs=1e-3)


def test_an_odd_byte_count_does_not_crash():
    """A frame can be cut mid-sample; dropping the stray byte beats raising."""
    samples = pcm16_to_float32(b"\x00\x00\x01")
    assert len(samples) == 1


# --- Behaviour 5: off the event loop --------------------------------------


@pytest.mark.asyncio
async def test_decoding_runs_off_the_event_loop():
    engine = FakeRecogniser(script=[("你", False)])
    recogniser = make(engine)
    await recogniser.start()
    await feed(recogniser)

    assert engine.decode_thread_ids, "decode_stream was never called"
    assert threading.get_ident() not in engine.decode_thread_ids


@pytest.mark.asyncio
async def test_the_event_loop_keeps_running_while_decoding():
    engine = FakeRecogniser(
        script=[("a%d" % i, False) for i in range(5)], decode_delay=0.03
    )
    recogniser = make(engine)
    await recogniser.start()

    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        await feed(recogniser, frames=5)
    finally:
        ticker.cancel()

    assert ticks > 5


# --- Behaviour 6: partials only when they change --------------------------


@pytest.mark.asyncio
async def test_a_partial_is_emitted_when_the_text_changes():
    engine = FakeRecogniser(script=[("你", False), ("你好", False)])
    recogniser = make(engine)
    await recogniser.start()

    results = await feed(recogniser, frames=2)

    assert [(r.text, r.final) for r in results] == [
        ("你", False),
        ("你好", False),
    ]


@pytest.mark.asyncio
async def test_an_unchanged_partial_is_not_emitted_again():
    """Every repeat drove an interrupt downstream on the board."""
    engine = FakeRecogniser(
        script=[("你好", False), ("你好", False), ("你好", False)]
    )
    recogniser = make(engine)
    await recogniser.start()

    results = await feed(recogniser, frames=3)

    assert len(results) == 1
    assert results[0].text == "你好"


@pytest.mark.asyncio
async def test_empty_text_is_never_emitted():
    engine = FakeRecogniser(script=[("", False), ("", False)])
    recogniser = make(engine)
    await recogniser.start()

    assert await feed(recogniser, frames=2) == []


# --- Behaviour 7: endpoints -----------------------------------------------


@pytest.mark.asyncio
async def test_an_endpoint_emits_a_final_and_resets_the_stream():
    engine = FakeRecogniser(script=[("你好", False), ("你好嗎", True)])
    recogniser = make(engine)
    await recogniser.start()

    results = await feed(recogniser, frames=2)

    assert [(r.text, r.final) for r in results] == [
        ("你好", False),
        ("你好嗎", True),
    ]
    assert engine.resets == 1


@pytest.mark.asyncio
async def test_the_next_utterance_starts_clean_after_an_endpoint():
    engine = FakeRecogniser(script=[("第一句", True), ("第二句", False)])
    recogniser = make(engine)
    await recogniser.start()

    results = await feed(recogniser, frames=2)

    assert [(r.text, r.final) for r in results] == [
        ("第一句", True),
        ("第二句", False),
    ]


@pytest.mark.asyncio
async def test_an_endpoint_with_no_text_is_not_emitted():
    """Silence long enough to end an utterance that never had words."""
    engine = FakeRecogniser(script=[("", True)])
    recogniser = make(engine)
    await recogniser.start()

    assert await feed(recogniser) == []
    assert engine.resets == 1


# --- Behaviour 8: finalize ------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_emits_a_final_without_an_endpoint():
    engine = FakeRecogniser(script=[("講到一半", False)])
    recogniser = make(engine)
    await recogniser.start()
    await feed(recogniser)

    results = await recogniser.finalize()

    assert [(r.text, r.final) for r in results] == [("講到一半", True)]


@pytest.mark.asyncio
async def test_finalize_after_an_endpoint_emits_nothing():
    engine = FakeRecogniser(script=[("說完了", True)])
    recogniser = make(engine)
    await recogniser.start()
    await feed(recogniser)

    assert await recogniser.finalize() == []


@pytest.mark.asyncio
async def test_finalize_closes_the_engine_stream():
    engine = FakeRecogniser(script=[("話", False)])
    recogniser = make(engine)
    await recogniser.start()
    await feed(recogniser)
    await recogniser.finalize()

    assert engine.streams[0].finished is True


# --- Timing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_timing_comes_from_the_audio_not_the_clock():
    engine = FakeRecogniser(script=[("一", False), ("一二", True)])
    recogniser = make(engine)
    await recogniser.start()

    # A real pause between frames, so a wall-clock stamp would show it.
    results = await feed(recogniser)
    await asyncio.sleep(0.3)
    results += await feed(recogniser)

    # Two 20 ms frames of audio, whatever the clock did in between.
    assert results[-1].final is True
    assert results[-1].start_ms == 0
    assert results[-1].duration_ms == 40


# --- Behaviour 9 and 10: lifecycle ----------------------------------------


@pytest.mark.asyncio
async def test_the_model_loads_once():
    loads = []

    def load(_config):
        loads.append(1)
        return FakeRecogniser(script=[("x", False)])

    recogniser = SherpaOnnxRecogniser(
        config=SherpaOnnxASRConfig(model_dir="/unused"),
        ten_env=FakeTenEnv(),
        load_engine=load,
    )
    await recogniser.start()
    await recogniser.start()
    await feed(recogniser)

    assert len(loads) == 1


@pytest.mark.asyncio
async def test_audio_before_the_model_is_ready_is_not_lost():
    """The base class buffers, but a load in flight must not drop frames."""
    ready = asyncio.Event()

    def load(_config):
        # Blocks in the worker thread until the test lets it finish.
        while not ready.is_set():
            import time

            time.sleep(0.005)
        return FakeRecogniser(script=[("遲到的字", False)])

    recogniser = SherpaOnnxRecogniser(
        config=SherpaOnnxASRConfig(model_dir="/unused"),
        ten_env=FakeTenEnv(),
        load_engine=load,
    )
    starting = asyncio.create_task(recogniser.start())
    await asyncio.sleep(0.02)
    ready.set()
    await starting

    results = await feed(recogniser)
    assert [r.text for r in results] == ["遲到的字"]


@pytest.mark.asyncio
async def test_stop_releases_the_model():
    engine = FakeRecogniser(script=[("x", False)])
    recogniser = make(engine)
    await recogniser.start()
    await recogniser.stop()

    assert recogniser.engine is None


@pytest.mark.asyncio
async def test_a_model_that_will_not_load_is_reported_not_raised():
    def load(_config):
        raise RuntimeError("tokens.txt is missing")

    ten_env = FakeTenEnv()
    recogniser = SherpaOnnxRecogniser(
        config=SherpaOnnxASRConfig(model_dir="/unused"),
        ten_env=ten_env,
        load_engine=load,
    )
    with pytest.raises(RuntimeError, match="tokens.txt"):
        await recogniser.start()
    assert recogniser.engine is None
