#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The one claim this adapter takes from sherpa-onnx that its docs get wrong.

``OfflineTts.generate()`` documents its callback as "Return a non-zero value to
stop generation early". Measured against sherpa-onnx 1.13.8 the opposite is
true: zero stops and non-zero continues. Implementing it as documented cuts
every reply off after its first sentence -- no exception, no log line, just
speech that ends early, which is not a symptom anyone traces back to a return
value.

This runs only where a voice is present, which in practice means the board.
Point it at one:

  SHERPA_ONNX_TTS_VOICE_DIR=~/piper_tts/vits/vits-piper-zh_CN-huayan-medium \
      ./tests/bin/start -k engine_contract
"""

import os

import pytest

from sherpa_onnx_tts_python.config import SherpaOnnxTTSConfig
from sherpa_onnx_tts_python.const import CALLBACK_CONTINUE, CALLBACK_STOP
from sherpa_onnx_tts_python.sherpa_onnx_tts import load_vits_engine

VOICE_DIR = os.path.expanduser(os.environ.get("SHERPA_ONNX_TTS_VOICE_DIR", ""))

# Several sentences, because the callback fires once per sentence: with one
# sentence both polarities look identical.
TEXT = "你好。今天天氣很好。我們出去走走吧。"

pytestmark = pytest.mark.skipif(
    not VOICE_DIR or not os.path.isdir(VOICE_DIR),
    reason="set SHERPA_ONNX_TTS_VOICE_DIR to a sherpa-onnx voice bundle",
)


@pytest.fixture(scope="module")
def engine():
    return load_vits_engine(SherpaOnnxTTSConfig(voice_dir=VOICE_DIR))


def _synthesise(engine_, verdict):
    """Run one synthesis whose callback always returns ``verdict``."""
    calls = []

    def callback(samples, progress):
        calls.append((len(samples), progress))
        return verdict

    audio = engine_.generate(TEXT, sid=0, speed=1.0, callback=callback)
    return calls, audio


def test_returning_continue_synthesises_the_whole_text(engine):
    calls, audio = _synthesise(engine, CALLBACK_CONTINUE)

    assert len(calls) > 1, (
        "the engine stopped after one sentence while being told to continue; "
        f"CALLBACK_CONTINUE={CALLBACK_CONTINUE} is no longer the right value"
    )
    assert calls[-1][1] == pytest.approx(1.0)
    assert len(audio.samples) > 0


def test_returning_stop_ends_generation_after_one_sentence(engine):
    calls, _ = _synthesise(engine, CALLBACK_STOP)

    assert len(calls) == 1, (
        f"CALLBACK_STOP={CALLBACK_STOP} did not stop generation; barge-in "
        "would keep synthesising a reply the listener has interrupted"
    )
    assert calls[0][1] < 1.0


def test_the_model_reports_its_own_rate(engine):
    """Behaviour 5: the rate is read, never assumed."""
    assert engine.sample_rate > 0
