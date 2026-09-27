#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""What the transcriber owes the record: who spoke, when, and what they said.

The merge is the whole point. Diarization knows when each person spoke and
recognition knows what was said; neither alone is a meeting record.
"""

import numpy as np
import pytest

from meeting_transcriber.config import MeetingTranscriberConfig
from meeting_transcriber.transcriber import MeetingTranscriber, read_pcm16

from .fakes import FakeDiarizer, FakeRecogniser, FakeSegment, FakeTenEnv


def make(segments, texts, **overrides):
    overrides.setdefault("segmentation_model", "/unused")
    overrides.setdefault("embedding_model", "/unused")
    overrides.setdefault("asr_model_dir", "/unused")
    return MeetingTranscriber(
        config=MeetingTranscriberConfig(**overrides),
        ten_env=FakeTenEnv(),
        load_diarizer=lambda _c: FakeDiarizer(segments),
        load_recogniser=lambda _c: FakeRecogniser(texts),
    )


@pytest.mark.asyncio
async def test_each_speaker_turn_gets_its_own_text(tmp_path):
    pcm = tmp_path / "segment.pcm"
    pcm.write_bytes(b"\x00\x01" * 16000 * 10)  # 10 s

    transcriber = make(
        segments=[
            FakeSegment(0.5, 3.0, 0),
            FakeSegment(3.2, 6.0, 1),
            FakeSegment(6.4, 9.0, 0),
        ],
        texts=["下週要出版本。", "測試需要三天。", "那就下週二看。"],
    )
    out = await transcriber.transcribe(str(pcm), speakers=2)

    assert [u.speaker for u in out] == [0, 1, 0]
    assert [u.text for u in out] == [
        "下週要出版本。",
        "測試需要三天。",
        "那就下週二看。",
    ]
    assert out[1].start_s == pytest.approx(3.2)


@pytest.mark.asyncio
async def test_a_segment_with_nobody_speaking_returns_nothing(tmp_path):
    """VAD opens on a cough or a chair. An empty answer, not an exception."""
    pcm = tmp_path / "quiet.pcm"
    pcm.write_bytes(b"\x00\x00" * 16000 * 3)

    transcriber = make(segments=[], texts=[])
    assert await transcriber.transcribe(str(pcm), speakers=2) == []


@pytest.mark.asyncio
async def test_an_empty_file_returns_nothing_without_loading_a_model(
    tmp_path,
):
    pcm = tmp_path / "empty.pcm"
    pcm.write_bytes(b"")

    transcriber = make(segments=[FakeSegment(0, 1, 0)], texts=["never"])
    assert await transcriber.transcribe(str(pcm), speakers=2) == []


@pytest.mark.asyncio
async def test_a_turn_reaching_past_the_end_is_clipped(tmp_path):
    """Diarization rounds; the samples do not have to be there."""
    pcm = tmp_path / "short.pcm"
    pcm.write_bytes(b"\x00\x01" * 16000 * 2)  # 2 s

    transcriber = make(segments=[FakeSegment(1.0, 9.0, 0)], texts=["夠了。"])
    out = await transcriber.transcribe(str(pcm), speakers=1)
    assert [u.text for u in out] == ["夠了。"]


@pytest.mark.asyncio
async def test_the_speaker_count_reaches_the_clusterer(tmp_path):
    """Without it the same person comes back as several -- four found as seven
    on the board. The number has to arrive."""
    seen = {}

    def spy(config):
        seen["speakers"] = config.speakers
        return FakeDiarizer([FakeSegment(0, 1, 0)])

    pcm = tmp_path / "s.pcm"
    pcm.write_bytes(b"\x00\x01" * 16000)
    transcriber = MeetingTranscriber(
        config=MeetingTranscriberConfig(
            segmentation_model="/unused",
            embedding_model="/unused",
            asr_model_dir="/unused",
        ),
        ten_env=FakeTenEnv(),
        load_diarizer=spy,
        load_recogniser=lambda _c: FakeRecogniser(["好。"]),
    )
    await transcriber.transcribe(str(pcm), speakers=3)
    assert seen["speakers"] == 3
