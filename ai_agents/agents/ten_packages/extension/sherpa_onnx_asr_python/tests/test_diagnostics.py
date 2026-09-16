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
async def test_a_decoder_falling_behind_says_so_once_and_not_per_frame():
    """The start-up backlog was 1.65 s and took six lines of algebra to see.

    Edge-triggered: a level would put a line on every frame of the drain,
    which is 50 a second and reads as noise rather than as a backlog.
    """
    clock = FakeClock()
    engine = FakeRecogniser(script=[("讲", False)] * 40)
    rec, env = await make(engine, clock=clock)

    # A second of wall time with no audio accepted: the model was loading.
    clock.advance_ms(1000)

    for _ in range(10):
        clock.advance_ms(FRAME_MS)
        await rec.accept(FRAME)

    behind = lines_with(env, "behind the audio")
    assert len(behind) == 1, behind
    assert "1000 ms behind the audio" in behind[0]
    assert lines_with(env, "caught up") == []


@pytest.mark.asyncio
async def test_catching_up_is_reported_so_the_backlog_has_an_end():
    clock = FakeClock()
    engine = FakeRecogniser(script=[("讲", False)] * 200)
    rec, env = await make(engine, clock=clock)

    clock.advance_ms(1000)
    await rec.accept(FRAME)
    assert len(lines_with(env, "behind the audio")) == 1

    # Frames arriving faster than real time, which is how the base class
    # replays what it buffered while the model loaded.
    for _ in range(60):
        await rec.accept(FRAME)

    (line,) = lines_with(env, "caught up")
    assert "caught up" in line
    # And it does not then say it again on every later frame.
    for _ in range(10):
        await rec.accept(FRAME)
    assert len(lines_with(env, "caught up")) == 1


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
