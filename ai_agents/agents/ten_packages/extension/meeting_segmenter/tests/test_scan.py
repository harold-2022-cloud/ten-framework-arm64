#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Where the speech is, in seconds of the file, read off the decoded PCM."""

import numpy as np

from meeting_segmenter.segmenter import scan

from .fakes import WINDOW, ScriptedVad


def write_pcm(path, samples):
    np.asarray(samples, dtype=np.int16).tofile(path)


def test_speech_windows_become_runs_in_seconds(tmp_path):
    pcm = tmp_path / "audio.pcm"
    write_pcm(pcm, np.zeros(5 * WINDOW))
    vad = ScriptedVad([False, True, True, False, True])

    assert scan(str(pcm), vad) == [(0.1, 0.3), (0.4, 0.5)]


def test_the_vad_hears_floats_in_unit_range(tmp_path):
    pcm = tmp_path / "audio.pcm"
    write_pcm(pcm, np.full(WINDOW, 16384))
    vad = ScriptedVad([True])

    scan(str(pcm), vad)

    assert vad.windows[0].dtype == np.float32
    assert float(vad.windows[0][0]) == 0.5


def test_a_trailing_partial_window_is_not_fed_to_the_vad(tmp_path):
    pcm = tmp_path / "audio.pcm"
    write_pcm(pcm, np.zeros(2 * WINDOW + WINDOW // 2))
    vad = ScriptedVad([False, False, True])

    scan(str(pcm), vad)

    assert [len(w) for w in vad.windows] == [WINDOW, WINDOW]
