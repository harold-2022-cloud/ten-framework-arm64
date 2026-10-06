#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""What segments_ready carries: the decoded PCM, its length, the topics."""

import numpy as np
import soundfile as sf

from meeting_segmenter.config import MeetingSegmenterConfig
from meeting_segmenter.segmenter import segment_file

from .fakes import ScriptedVad

CONFIG = MeetingSegmenterConfig(
    segment_silence_s=0.5, min_segment_s=0.2, max_segment_s=10.0
)


def write_opus(path, seconds):
    t = np.arange(int(seconds * 16000)) / 16000
    sf.write(
        str(path),
        0.3 * np.sin(2 * np.pi * 440 * t),
        16000,
        format="OGG",
        subtype="OPUS",
    )


def test_each_topic_is_named_and_placed_in_file_seconds(tmp_path):
    ogg = tmp_path / "audio.ogg"
    write_opus(ogg, seconds=3.0)
    # 0.1 s windows: speech 0-1 s, quiet 1-2 s, speech 2-3 s.
    vad = ScriptedVad([True] * 10 + [False] * 10 + [True] * 10)

    payload = segment_file(str(ogg), str(tmp_path), CONFIG, lambda: vad)

    assert payload == {
        "pcm_path": str(tmp_path / "audio.pcm"),
        "duration_s": 3.0,
        "segments": [
            {"id": "t01", "start_s": 0.0, "end_s": 1.5},
            {"id": "t02", "start_s": 1.5, "end_s": 3.0},
        ],
        "error": None,
    }
    assert (tmp_path / "audio.pcm").stat().st_size == 3 * 16000 * 2


def test_a_file_that_will_not_decode_is_reported_not_raised(tmp_path):
    ogg = tmp_path / "audio.ogg"
    ogg.write_bytes(b"this is not an ogg file")

    payload = segment_file(
        str(ogg), str(tmp_path), CONFIG, lambda: ScriptedVad([])
    )

    assert payload["segments"] == []
    assert payload["error"]
