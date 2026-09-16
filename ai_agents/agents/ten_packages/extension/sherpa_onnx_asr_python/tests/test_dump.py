#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The captured audio has to be worth capturing.

The point of the dump is to answer questions the log cannot: whether a final
came out early, what the speaker actually said. That only works if the file
holds exactly the samples the recogniser was given, in order, on the same
clock as the timestamps on the results.
"""

import os

import pytest

from sherpa_onnx_asr_python.config import SherpaOnnxASRConfig
from sherpa_onnx_asr_python.const import SAMPLE_RATE
from sherpa_onnx_asr_python.extension import SherpaOnnxASRExtension
from sherpa_onnx_asr_python.recogniser import SherpaOnnxRecogniser

from .fakes import FakeRecogniser, FakeTenEnv

FRAME_SAMPLES = 320  # 20 ms at 16 kHz
FRAME = b"\x11\x22" * FRAME_SAMPLES


class FakeAudioFrame:
    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm

    def lock_buf(self) -> bytes:
        return self.pcm

    def unlock_buf(self, _buf) -> None:
        pass


async def make_extension(tmp_path, engine=None, **overrides):
    """An extension taken through on_init, with a scripted engine attached."""
    properties = {
        "model_dir": str(tmp_path),
        "dump": True,
        "dump_path": str(tmp_path),
    }
    properties.update(overrides)

    ext = SherpaOnnxASRExtension("stt")
    env = FakeTenEnv(properties)
    await ext.on_init(env)

    config = SherpaOnnxASRConfig(**properties)
    ext.recogniser = SherpaOnnxRecogniser(
        config=config, ten_env=env, load_engine=lambda _c: engine
    )
    if engine is not None:
        await ext.recogniser.start()

    results = []

    async def record(result):
        results.append(result)

    ext.send_asr_result = record
    return ext, env, results


def dumps_in(tmp_path):
    return sorted(p for p in os.listdir(tmp_path) if p.endswith(".pcm"))


@pytest.mark.asyncio
async def test_nothing_is_written_when_the_dump_is_off(tmp_path):
    ext, _env, _ = await make_extension(
        tmp_path, FakeRecogniser(script=[]), dump=False
    )
    assert ext.audio_dumper is None
    await ext.send_audio(FakeAudioFrame(FRAME), None)
    assert dumps_in(tmp_path) == []


@pytest.mark.asyncio
async def test_the_file_holds_every_sample_the_engine_was_given(tmp_path):
    engine = FakeRecogniser(script=[])
    ext, _env, _ = await make_extension(tmp_path, engine)

    for _ in range(5):
        await ext.send_audio(FakeAudioFrame(FRAME), None)
    await ext.on_deinit(FakeTenEnv())

    (name,) = dumps_in(tmp_path)
    written = open(os.path.join(tmp_path, name), "rb").read()
    assert written == FRAME * 5
    assert len(written) // 2 == ext.recogniser._samples_accepted


@pytest.mark.asyncio
async def test_a_byte_offset_is_a_position_in_the_transcript(tmp_path):
    """What makes the capture usable: the file and the results share a clock.

    A final at start_ms=31130 duration_ms=3200 has to name the bytes at
    (31130 + 3200) / 1000 * 16000 * 2 in the file, or the audio cannot be
    used to check where the endpoint fired.
    """
    engine = FakeRecogniser(
        script=[("再讲讲一个", False)] * 4 + [("再讲讲一个", True)]
    )
    ext, _env, results = await make_extension(tmp_path, engine)

    for _ in range(5):
        await ext.send_audio(FakeAudioFrame(FRAME), None)
    await ext.on_deinit(FakeTenEnv())

    final = [r for r in results if r.final][-1]
    (name,) = dumps_in(tmp_path)
    size = os.path.getsize(os.path.join(tmp_path, name))

    end_ms = final.start_ms + final.duration_ms
    assert size == end_ms * SAMPLE_RATE * 2 // 1000


@pytest.mark.asyncio
async def test_a_frame_cut_mid_sample_does_not_shift_the_file(tmp_path):
    """One stray byte would put every later offset half a sample out."""
    engine = FakeRecogniser(script=[])
    ext, _env, _ = await make_extension(tmp_path, engine)

    await ext.send_audio(FakeAudioFrame(FRAME + b"\x77"), None)
    await ext.send_audio(FakeAudioFrame(FRAME), None)
    await ext.on_deinit(FakeTenEnv())

    (name,) = dumps_in(tmp_path)
    written = open(os.path.join(tmp_path, name), "rb").read()
    assert written == FRAME * 2
    assert len(written) // 2 == ext.recogniser._samples_accepted


@pytest.mark.asyncio
async def test_an_unwritable_path_disables_the_dump_and_not_the_asr(tmp_path):
    """A debugging aid may not be able to end the session."""
    engine = FakeRecogniser(script=[("讲一个笑话", True)])
    ext, env, results = await make_extension(
        tmp_path, engine, dump_path=str(tmp_path / "no" / "such" / "\0bad")
    )

    assert ext.audio_dumper is None
    assert any("audio dump disabled" in line for line in env.lines)

    await ext.send_audio(FakeAudioFrame(FRAME), None)
    assert [r.text for r in results] == ["讲一个笑话"]


@pytest.mark.asyncio
async def test_a_long_capture_arrives_whole_and_in_order(tmp_path):
    """Six seconds of audio, well past any buffering in the writer.

    The end of the file is the part the capture exists to explain -- it is
    where the endpoint fired -- so losing or reordering the tail would make
    the recording worse than none.
    """
    engine = FakeRecogniser(script=[])
    ext, _env, _ = await make_extension(tmp_path, engine)

    frames = 300  # 192000 bytes, three times the 64 KB buffer
    for _ in range(frames):
        await ext.send_audio(FakeAudioFrame(FRAME), None)
    await ext.on_deinit(FakeTenEnv())

    (name,) = dumps_in(tmp_path)
    written = open(os.path.join(tmp_path, name), "rb").read()
    assert len(written) == len(FRAME) * frames
    assert written == FRAME * frames
