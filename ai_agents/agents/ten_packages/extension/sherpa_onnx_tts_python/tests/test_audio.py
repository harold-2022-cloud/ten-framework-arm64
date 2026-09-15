#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Converting the voice's rate to the one the RTC chain was declared with.

agora_rtc states its PCM rate once, when the service starts, so every frame
after that has to arrive at it however the voice was trained. The Mandarin
Piper voice is 22050 Hz; some English ones are 16000.
"""

import numpy as np

from sherpa_onnx_tts_python.audio import float_to_pcm16, resample_pcm16


def test_a_voice_already_at_the_target_rate_is_untouched():
    pcm = np.arange(-100, 100, dtype=np.int16).tobytes()
    assert resample_pcm16(pcm, 16000, 16000) is pcm


def test_downsampling_preserves_duration():
    one_second = np.zeros(22050, dtype=np.int16).tobytes()
    assert len(resample_pcm16(one_second, 22050, 16000)) // 2 == 16000


def test_downsampling_filters_rather_than_dropping_samples():
    # A 9 kHz tone is above the 8 kHz Nyquist limit of the target rate. Naive
    # decimation folds it back into the audible band; resample_poly's FIR
    # filter removes it instead. The result should be near silence, not a
    # loud alias.
    t = np.arange(22050) / 22050.0
    tone = (np.sin(2 * np.pi * 9000 * t) * 20000).astype(np.int16)
    out = np.frombuffer(resample_pcm16(tone.tobytes(), 22050, 16000), np.int16)
    assert np.abs(out).mean() < 2000, "the 9 kHz tone aliased through"


def test_float_samples_become_clipped_pcm16():
    # sherpa-onnx hands back float32 in [-1, 1], and a voice can overshoot it.
    samples = np.array([0.0, 1.0, -1.0, 2.0, -2.0], dtype=np.float32)
    out = np.frombuffer(float_to_pcm16(samples), np.int16)
    assert out.tolist() == [0, 32767, -32767, 32767, -32767]
