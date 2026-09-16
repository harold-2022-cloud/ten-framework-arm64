#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""What the real engine does, which a fake cannot tell us.

The unit tests drive a stand-in built to sherpa-onnx's contract. These check
the contract itself against a model, because two of its properties decide
whether a conversation works at all: that the bundle loads without being told
which file is which, and that a pause produces a final result without anyone
asking for one.

The second is the failure that stopped the board's sessions. Soniox held a
question indefinitely when the speaker used no sentence-ending punctuation --
讲一个笑话 waited ninety-seven seconds -- because its holding mode waits for a
terminator. Endpointing waits for silence instead, and owes nothing to
punctuation.

Needs a model, so it skips unless one is named:

  SHERPA_ONNX_ASR_MODEL_DIR=~/zipformer_asr/models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20 \
      ./tests/bin/start -k engine_contract
"""

import asyncio
import glob
import os
import wave

import pytest

from sherpa_onnx_asr_python.config import SherpaOnnxASRConfig
from sherpa_onnx_asr_python.recogniser import (
    SherpaOnnxRecogniser,
    load_zipformer_engine,
)

MODEL_DIR = os.path.expanduser(os.environ.get("SHERPA_ONNX_ASR_MODEL_DIR", ""))

pytestmark = pytest.mark.skipif(
    not MODEL_DIR or not os.path.isdir(MODEL_DIR),
    reason="set SHERPA_ONNX_ASR_MODEL_DIR to a sherpa-onnx transducer bundle",
)


class _Env:
    def _quiet(self, *_args, **_kwargs):
        pass

    log_debug = log_info = log_warn = log_error = _quiet


def _config(**overrides):
    return SherpaOnnxASRConfig(model_dir=MODEL_DIR, num_threads=1, **overrides)


@pytest.fixture(scope="module")
def engine():
    return load_zipformer_engine(_config())


def test_the_bundle_loads_without_being_told_which_file_is_which(engine):
    """The four files are named after the training run, not their role."""
    assert engine is not None
    for pattern in ("encoder-*.onnx", "decoder-*.onnx", "joiner-*.onnx"):
        assert glob.glob(os.path.join(MODEL_DIR, pattern)), pattern
    assert os.path.isfile(os.path.join(MODEL_DIR, "tokens.txt"))


def _speech():
    """Any 16 kHz WAV the bundle ships with, plus three seconds of silence."""
    wavs = sorted(glob.glob(os.path.join(MODEL_DIR, "test_wavs", "*.wav")))
    if not wavs:
        pytest.skip("the bundle carries no test_wavs")
    with wave.open(wavs[0], "rb") as src:
        if src.getframerate() != 16000:
            pytest.skip("the bundle's test audio is not 16 kHz")
        frames = src.readframes(src.getnframes())
    return frames, len(frames) / 2 / 16000


async def _feed(recogniser, frames):
    """Twenty milliseconds at a time, the way RTC delivers audio."""
    out = []
    step = 320 * 2
    for offset in range(0, len(frames), step):
        out.extend(await recogniser.accept(frames[offset : offset + step]))
    return out


@pytest.mark.asyncio
async def test_a_pause_produces_a_final_without_anyone_asking():
    speech, speech_seconds = _speech()
    silence = b"\x00\x00" * (16000 * 3)

    recogniser = SherpaOnnxRecogniser(config=_config(), ten_env=_Env())
    await recogniser.start()
    results = await _feed(recogniser, speech + silence)
    await recogniser.stop()

    finals = [r for r in results if r.final]
    assert finals, (
        "three seconds of silence produced no final. A conversation cannot "
        "work: nothing reaches the model until the turn is torn down."
    )
    assert finals[0].text.strip()


@pytest.mark.asyncio
async def test_no_pause_produces_no_final_until_finalize():
    """Mid-sentence, the transcript is provisional and stays that way."""
    speech, _ = _speech()

    recogniser = SherpaOnnxRecogniser(config=_config(), ten_env=_Env())
    await recogniser.start()
    during = await _feed(recogniser, speech)
    closing = await recogniser.finalize()
    await recogniser.stop()

    assert not [r for r in during if r.final]
    assert [r for r in closing if r.final]


@pytest.mark.asyncio
async def test_turning_endpointing_off_leaves_it_to_finalize():
    speech, _ = _speech()
    silence = b"\x00\x00" * (16000 * 3)

    recogniser = SherpaOnnxRecogniser(
        config=_config(enable_endpoint_detection=False), ten_env=_Env()
    )
    await recogniser.start()
    results = await _feed(recogniser, speech + silence)
    await recogniser.stop()

    assert not [r for r in results if r.final]


@pytest.mark.asyncio
async def test_partials_arrive_while_the_speaker_is_still_talking():
    speech, _ = _speech()

    recogniser = SherpaOnnxRecogniser(config=_config(), ten_env=_Env())
    await recogniser.start()
    results = await _feed(recogniser, speech)
    await recogniser.stop()

    partials = [r for r in results if not r.final]
    assert len(partials) > 1
    # They grow; the adapter suppresses the repeats.
    assert len(partials[-1].text) > len(partials[0].text)
