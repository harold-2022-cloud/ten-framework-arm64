#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The engine knows things the log did not say.

Reading task_run.log meant reconstructing, from six lines of start_ms and
duration_ms, which rule ended each utterance and how far behind real time the
decoder was. Both were quantities the adapter held at the time. These tests
are about saying them.
"""

import pytest

from sherpa_onnx_asr_python.config import SherpaOnnxASRConfig
from sherpa_onnx_asr_python.recogniser import SherpaOnnxRecogniser

from .fakes import FakeRecogniser, FakeTenEnv

FRAME_SAMPLES = 320  # 20 ms at 16 kHz, one decode step in FakeRecogniser
FRAME_MS = 20
FRAME = b"\x00\x01" * FRAME_SAMPLES


class FakeClock:
    """A clock the test moves by hand, so lag is a number and not a race."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0


async def make(engine, clock=None, **overrides):
    overrides.setdefault("model_dir", "/unused")
    env = FakeTenEnv()
    rec = SherpaOnnxRecogniser(
        config=SherpaOnnxASRConfig(**overrides),
        ten_env=env,
        load_engine=lambda _c: engine,
        clock=clock or FakeClock(),
    )
    await rec.start()
    return rec, env


def lines_with(env, needle):
    return [line for line in env.lines if needle in line]


@pytest.mark.asyncio
async def test_an_endpoint_says_how_much_silence_ended_the_utterance():
    """The number that tells rule1 from rule2 without owning the rules.

    Text stops changing after the second frame and the endpoint lands on the
    eighth, so six frames -- 120 ms -- of audio carried no new text. That is
    the quantity that was 1280 ms on the board and had to be subtracted out
    of two timestamps to find.
    """
    engine = FakeRecogniser(
        script=[("讲", False), ("讲一个笑话", False)]
        + [("讲一个笑话", False)] * 5
        + [("讲一个笑话", True)]
    )
    rec, env = await make(engine)

    for _ in range(8):
        await rec.accept(FRAME)

    (line,) = lines_with(env, "endpoint after")
    assert "endpoint after 120 ms of trailing silence" in line
    assert "utterance=160 ms" in line
    assert "text='讲一个笑话'" in line


@pytest.mark.asyncio
async def test_the_endpoint_line_counts_the_decodes_behind_each_utterance():
    """Two partials reached the graph out of eight decodes, on the board.

    Whether the engine was quiet or the adapter was suppressing repeats was
    not answerable from the log; the decode count answers it.
    """
    engine = FakeRecogniser(
        script=[("讲", False)] + [("讲", False)] * 6 + [("讲", True)]
    )
    rec, env = await make(engine)

    for _ in range(8):
        await rec.accept(FRAME)

    (line,) = lines_with(env, "endpoint after")
    assert "decodes=8" in line


@pytest.mark.asyncio
async def test_the_backlog_at_the_start_is_reported_once():
    """The audio that piled up while the model loaded is worth one line."""
    clock = FakeClock()
    engine = FakeRecogniser(script=[("讲", False)] * 40)
    rec, env = await make(engine, clock=clock)

    clock.advance_ms(6500)  # the board's model load
    for _ in range(10):
        clock.advance_ms(FRAME_MS)
        await rec.accept(FRAME)

    started = lines_with(env, "decoding starts")
    assert len(started) == 1, started
    assert "6500 ms behind the audio" in started[0]
    assert lines_with(env, "falling behind") == []


@pytest.mark.asyncio
async def test_a_constant_offset_is_never_reported_as_falling_behind():
    """Replayed from task_run.log 11:41-11:44: 674 ms, flat, for three minutes.

    The decoder was keeping up exactly -- a constant lag is an offset, not a
    deficit, because one that cannot keep up accumulates. Comparing the raw
    number against a threshold called that a backlog, latched, and then could
    not report the real thing if it happened.
    """
    clock = FakeClock()
    engine = FakeRecogniser(script=[("讲", False)] * 400)
    rec, env = await make(engine, clock=clock)

    clock.advance_ms(674)  # whatever separates capture from start()
    for _ in range(200):
        clock.advance_ms(FRAME_MS)
        await rec.accept(FRAME)

    assert lines_with(env, "falling behind") == []
    assert lines_with(env, "caught up") == []


@pytest.mark.asyncio
async def test_drifting_further_than_the_floor_is_reported_once_then_cleared():
    """What the measure is actually for: the decoder losing ground."""
    clock = FakeClock()
    engine = FakeRecogniser(script=[("讲", False)] * 400)
    rec, env = await make(engine, clock=clock)

    clock.advance_ms(674)
    for _ in range(20):  # settle, establishing the floor
        clock.advance_ms(FRAME_MS)
        await rec.accept(FRAME)
    assert lines_with(env, "falling behind") == []

    # Wall time running at twice the audio: each frame loses 20 ms, so 40 of
    # them put 800 ms between the decoder and its own best.
    for _ in range(40):
        clock.advance_ms(FRAME_MS * 2)
        await rec.accept(FRAME)

    behind = lines_with(env, "falling behind")
    assert len(behind) == 1, behind
    assert "further than its best" in behind[0]

    # Frames arriving with no wall time passing, which is the buffer being
    # replayed: each one gives 20 ms back.
    for _ in range(40):
        await rec.accept(FRAME)

    assert len(lines_with(env, "caught up")) == 1
    assert len(lines_with(env, "falling behind")) == 1


@pytest.mark.asyncio
async def test_silence_endpoints_are_not_logged():
    """64 of 67 lines on the board said the same thing about quiet.

    rule1 ends an utterance on every 2.56 s of silence whether anyone spoke
    or not, and each one carried utterance == trailing and nothing else.
    """
    engine = FakeRecogniser(
        script=[
            ("", True),
            ("", True),
            ("讲一个笑话", False),
            ("讲一个笑话", True),
        ]
    )
    rec, env = await make(engine)

    for _ in range(4):
        await rec.accept(FRAME)

    (line,) = lines_with(env, "endpoint after")
    assert "text='讲一个笑话'" in line


@pytest.mark.asyncio
async def test_silent_endpoints_do_not_inflate_the_next_decode_count():
    """The counter belongs to an utterance, not to the session."""
    engine = FakeRecogniser(
        script=[("", True), ("", True), ("讲", False), ("讲", True)]
    )
    rec, env = await make(engine)

    for _ in range(4):
        await rec.accept(FRAME)

    (line,) = lines_with(env, "endpoint after")
    assert "decodes=2" in line


@pytest.mark.asyncio
async def test_finalize_says_the_utterance_ended_without_an_endpoint():
    """Who ended the turn, the engine or main_control, was not in the log.

    They mean different things: one is the speaker stopping, the other is the
    graph deciding not to wait.
    """
    engine = FakeRecogniser(script=[("讲一个笑话", False)] * 3)
    rec, env = await make(engine)

    for _ in range(3):
        await rec.accept(FRAME)
    out = await rec.finalize()

    assert [t.final for t in out] == [True]
    (line,) = lines_with(env, "finalize took the utterance")
    assert "60 ms of audio" in line
    assert "text='讲一个笑话'" in line


@pytest.mark.asyncio
async def test_a_second_utterance_measures_its_own_silence():
    """The counters reset at the endpoint, or every later reading is cumulative."""
    engine = FakeRecogniser(
        script=[("一", False), ("一", True), ("二", False), ("二", True)]
    )
    rec, env = await make(engine)

    for _ in range(4):
        await rec.accept(FRAME)

    # One frame of each utterance carried new text, the next ended it.
    first, second = lines_with(env, "endpoint after")
    assert "endpoint after 20 ms of trailing silence" in first
    assert "utterance=40 ms" in first
    assert "decodes=2" in first
    assert "endpoint after 20 ms of trailing silence" in second
    assert "utterance=40 ms" in second
    assert "decodes=2" in second
