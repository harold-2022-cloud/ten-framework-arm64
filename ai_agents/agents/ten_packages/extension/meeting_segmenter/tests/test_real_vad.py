#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The real TEN VAD through sherpa-onnx, where its model is installed.

Point MEETING_VAD_MODEL at ten-vad.onnx to run these; without it they skip
rather than pass, so a green run without the model claims nothing about it.
"""

import os

import numpy as np
import pytest

from meeting_segmenter.config import MeetingSegmenterConfig
from meeting_segmenter.segmenter import load_vad, scan

MODEL = os.environ.get("MEETING_VAD_MODEL", "")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(MODEL), reason="MEETING_VAD_MODEL not set to a file"
)


def test_the_real_model_hears_nobody_in_silence(tmp_path):
    pcm = tmp_path / "audio.pcm"
    np.zeros(16000 * 2, dtype=np.int16).tofile(pcm)

    vad = load_vad(MeetingSegmenterConfig(vad_model=MODEL))

    assert scan(str(pcm), vad) == []


def test_a_missing_model_is_named_in_the_error():
    with pytest.raises(ValueError, match="no-such-model.onnx"):
        load_vad(MeetingSegmenterConfig(vad_model="/tmp/no-such-model.onnx"))
