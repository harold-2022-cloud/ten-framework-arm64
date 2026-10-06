#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The uploaded Ogg-Opus becomes the PCM16 every later step reads.

Real files through the real codec: soundfile writes the Opus here and reads
it back, the same libsndfile the board uses.
"""

import numpy as np
import pytest
import soundfile as sf

from meeting_segmenter.segmenter import decode


def write_opus(path, seconds, rate=16000, channels=1):
    t = np.arange(int(seconds * rate)) / rate
    tone = 0.3 * np.sin(2 * np.pi * 440 * t)
    data = tone if channels == 1 else np.stack([tone] * channels, axis=1)
    sf.write(str(path), data, rate, format="OGG", subtype="OPUS")


def test_a_16k_mono_opus_decodes_to_pcm16_of_the_same_length(tmp_path):
    ogg, pcm = tmp_path / "audio.ogg", tmp_path / "audio.pcm"
    write_opus(ogg, seconds=2.0)

    samples = decode(str(ogg), str(pcm))

    assert samples == 32000
    assert pcm.stat().st_size == 64000


def test_a_48k_recording_is_refused_naming_its_rate(tmp_path):
    ogg = tmp_path / "audio.ogg"
    write_opus(ogg, seconds=1.0, rate=48000)

    with pytest.raises(ValueError, match="48000"):
        decode(str(ogg), str(tmp_path / "audio.pcm"))


def test_a_stereo_recording_is_refused(tmp_path):
    ogg = tmp_path / "audio.ogg"
    write_opus(ogg, seconds=1.0, channels=2)

    with pytest.raises(ValueError, match="2 channels"):
        decode(str(ogg), str(tmp_path / "audio.pcm"))
