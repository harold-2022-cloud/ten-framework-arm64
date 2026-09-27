#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A segment file is the meeting. Everything later reads it, so what is
written has to be exactly what arrived, aligned to whole samples."""

import os

import pytest

from meeting_recorder.recorder import SegmentWriter, whole_samples

FRAME = b"\x11\x22" * 320  # 20 ms at 16 kHz


def test_a_frame_cut_mid_sample_keeps_the_file_aligned(tmp_path):
    writer = SegmentWriter(str(tmp_path))
    writer.open(started_at=1000.0)
    writer.write(FRAME + b"\x77")
    writer.write(FRAME)
    segment = writer.close()

    written = open(segment.path, "rb").read()
    assert written == FRAME * 2, "a stray byte shifted every later sample"
    assert segment.bytes_written == len(FRAME) * 2


def test_closing_with_nothing_open_answers_instead_of_writing(tmp_path):
    """Two silences in a row, or a close after the meeting assembled."""
    writer = SegmentWriter(str(tmp_path))
    assert writer.close() is None
    assert writer.write(FRAME) == 0
    assert (
        os.listdir(tmp_path) == []
    ), "a file was created with nothing to record"


def test_duration_comes_from_the_bytes(tmp_path):
    """Not from the clock: the clock drifts against what was actually kept."""
    writer = SegmentWriter(str(tmp_path))
    writer.open(started_at=1000.0)
    for _ in range(50):  # 50 x 20 ms
        writer.write(FRAME)
    segment = writer.close()
    assert segment.duration_s == pytest.approx(1.0)


def test_an_unwritable_root_fails_at_open_not_at_minute_forty(tmp_path):
    """115 MB an hour. A meeting that dies of a full disk halfway through has
    lost the half nobody noticed."""
    writer = SegmentWriter(str(tmp_path / "no" / "such" / "\0bad"))
    with pytest.raises(Exception):
        writer.open(started_at=1000.0)
    assert not writer.is_open
