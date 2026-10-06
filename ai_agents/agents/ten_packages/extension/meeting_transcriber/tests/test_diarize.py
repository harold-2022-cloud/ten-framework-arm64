#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Diarization runs in a child process, so the worker's loop keeps running.

sherpa-onnx's OfflineSpeakerDiarization.process() holds the GIL for the
whole call (its binding has no gil_scoped_release). Run on an executor
thread inside the worker, it stopped every Python extension's shared loop
for the length of a topic -- 227 s for a six-minute one in the dev
container -- and with it the uploader's HTTP answers and the pings that
keep the worker from being reaped. A child process has its own GIL.

    DIARIZATION_SEG_MODEL, DIARIZATION_EMB_MODEL, SENSEVOICE_MODEL_DIR and
    MEETING_TEST_PCM (16 kHz mono PCM16 with speech in it) run the last test.
"""

import asyncio
import os
import time

import pytest

from meeting_transcriber.config import MeetingTranscriberConfig
from meeting_transcriber.diarize import diarize_slice
from meeting_transcriber.transcriber import (
    MeetingTranscriber,
    diarize_in_child,
)

from .fakes import FakeDiarizer, FakeSegment, FakeTenEnv

SEG = os.environ.get("DIARIZATION_SEG_MODEL", "")
EMB = os.environ.get("DIARIZATION_EMB_MODEL", "")
ASR = os.environ.get("SENSEVOICE_MODEL_DIR", "")
PCM = os.environ.get("MEETING_TEST_PCM", "")


def request(pcm, **overrides):
    out = {
        "pcm_path": str(pcm),
        "start_s": 0.0,
        "duration_s": None,
        "speakers": 2,
        "cluster_threshold": 0.5,
        "segmentation_model": "/unused",
        "embedding_model": "/unused",
        "num_threads": 1,
    }
    out.update(overrides)
    return out


def test_a_slice_is_diarized_from_its_own_samples(tmp_path):
    pcm = tmp_path / "meeting.pcm"
    pcm.write_bytes(b"\x00\x01" * 16000 * 2)
    diarizer = FakeDiarizer([FakeSegment(0.1, 0.4, 1)])

    turns = diarize_slice(
        request(pcm, start_s=1.0, duration_s=0.5), load=lambda _c: diarizer
    )

    assert diarizer.sample_counts == [8000]
    assert turns == [[0.1, 0.4, 1]]


def test_the_clusterer_is_built_for_this_meetings_head_count(tmp_path):
    pcm = tmp_path / "meeting.pcm"
    pcm.write_bytes(b"\x00\x01" * 16000)
    built = {}

    def spy(config):
        built.update(
            speakers=config.speakers, threshold=config.cluster_threshold
        )
        return FakeDiarizer([])

    diarize_slice(request(pcm, speakers=7, cluster_threshold=0.4), load=spy)

    assert built == {"speakers": 7, "threshold": 0.4}


@pytest.mark.asyncio
async def test_a_child_that_cannot_diarize_says_why_and_does_not_hang(
    tmp_path,
):
    pcm = tmp_path / "meeting.pcm"
    pcm.write_bytes(b"\x00\x01" * 16000)

    with pytest.raises(RuntimeError, match="diarization"):
        await asyncio.wait_for(
            diarize_in_child(
                request(
                    pcm,
                    segmentation_model="/missing/seg.onnx",
                    embedding_model="/missing/emb.onnx",
                )
            ),
            timeout=60,
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not all(p and os.path.exists(p) for p in (SEG, EMB, ASR, PCM)),
    reason="models and MEETING_TEST_PCM not set",
)
async def test_the_loop_keeps_running_while_topics_are_diarized():
    transcriber = MeetingTranscriber(
        MeetingTranscriberConfig(
            segmentation_model=SEG,
            embedding_model=EMB,
            asr_model_dir=ASR,
            num_threads=4,
        ),
        FakeTenEnv(),
    )
    stalls, done = [], False

    async def tick():
        last = time.monotonic()
        while not done:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            stalls.append(now - last)
            last = now

    task = asyncio.create_task(tick())
    # Two topics: in-process, the first call happened not to stall and every
    # later one did, so one topic would prove nothing.
    for start in (0.0, 60.0):
        await transcriber.transcribe(PCM, 4, start_s=start, duration_s=60.0)
    done = True
    await task

    assert max(stalls) < 2.0
