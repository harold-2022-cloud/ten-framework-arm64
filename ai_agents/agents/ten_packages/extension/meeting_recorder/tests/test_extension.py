#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""on_init decides whether this meeting gets recorded at all.

The disk guard has to fail closed before the first frame arrives, not
partway through a segment: by the time on_audio_frame runs, refusing is no
longer an option that saves anything.
"""

import pytest

from meeting_recorder.extension import MeetingRecorderExtension

from .fakes import FakeTenEnv


@pytest.mark.asyncio
async def test_a_too_small_disk_leaves_the_writer_off(tmp_path):
    """No real disk has an exabyte free, so this never depends on the host
    the suite happens to run on."""
    env = FakeTenEnv({"output_dir": str(tmp_path), "min_free_mb": 10**9})
    ext = MeetingRecorderExtension("meeting_recorder")

    await ext.on_init(env)

    assert ext.writer is None, "recording started despite the disk guard"
    assert any(
        "MB free" in line for line in env.lines
    ), "refusing to start left no trace in the log"


@pytest.mark.asyncio
async def test_a_sane_min_free_mb_leaves_the_writer_on(tmp_path):
    """Without this, the test above could pass for the wrong reason -- a
    writer that is never set regardless of min_free_mb."""
    env = FakeTenEnv({"output_dir": str(tmp_path), "min_free_mb": 1})
    ext = MeetingRecorderExtension("meeting_recorder")

    await ext.on_init(env)

    assert ext.writer is not None
