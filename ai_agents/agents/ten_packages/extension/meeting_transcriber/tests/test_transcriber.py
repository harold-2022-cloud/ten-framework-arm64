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
from meeting_transcriber.transcriber import (
    MeetingTranscriber,
    Utterance,
    read_pcm16,
    utterances_payload,
)

from .fakes import (
    in_process,
    FakeDiarizationResult,
    FakeDiarizer,
    FakeEmbeddingStream,
    FakeExtractor,
    FakeRecogniser,
    FakeSegment,
    FakeStream,
    FakeTenEnv,
)


def make(segments, texts, **overrides):
    overrides.setdefault("segmentation_model", "/unused")
    overrides.setdefault("embedding_model", "/unused")
    overrides.setdefault("asr_model_dir", "/unused")
    return MeetingTranscriber(
        config=MeetingTranscriberConfig(**overrides),
        ten_env=FakeTenEnv(),
        diarize=in_process(lambda _c: FakeDiarizer(segments)),
        load_extractor=lambda _c: FakeExtractor(),
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
        diarize=in_process(spy),
        load_extractor=lambda _c: FakeExtractor(),
        load_recogniser=lambda _c: FakeRecogniser(["好。"]),
    )
    await transcriber.transcribe(str(pcm), speakers=3)
    assert seen["speakers"] == 3


def test_the_fakes_expose_the_surface_the_real_engines_expose():
    """A fake modelling a method the real class does not have proves
    nothing -- that is exactly how get_result survived five green tests.
    Read the names off the installed sherpa-onnx rather than hardcoding
    what we expect it to look like, so a version mismatch fails here
    instead of on the board's first real segment."""
    sherpa_onnx = pytest.importorskip("sherpa_onnx")

    for name in ("create_stream", "decode_stream"):
        assert hasattr(sherpa_onnx.OfflineRecognizer, name), name
        assert hasattr(FakeRecogniser, name), name

    for name in ("accept_waveform", "result"):
        assert hasattr(sherpa_onnx.OfflineStream, name), name
        assert hasattr(FakeStream, name), name

    assert hasattr(sherpa_onnx.OfflineSpeakerDiarization, "process")
    assert hasattr(FakeDiarizer, "process")

    assert hasattr(
        sherpa_onnx.OfflineSpeakerDiarizationResult, "sort_by_start_time"
    )
    assert hasattr(FakeDiarizationResult, "sort_by_start_time")

    for name in ("create_stream", "is_ready", "compute"):
        assert hasattr(sherpa_onnx.SpeakerEmbeddingExtractor, name), name
        assert hasattr(FakeExtractor, name), name
    for name in ("accept_waveform", "input_finished"):
        assert hasattr(sherpa_onnx.OnlineStream, name), name
        assert hasattr(FakeEmbeddingStream, name), name


def write_counting_pcm(path, seconds):
    """Each sample's value is its own index, so a slice names itself."""
    np.arange(int(seconds * 16000), dtype=np.int16).tofile(path)


def test_a_slice_reads_only_its_own_samples(tmp_path):
    pcm = tmp_path / "meeting.pcm"
    write_counting_pcm(pcm, seconds=2.0)

    samples = read_pcm16(str(pcm), start_s=1.0, duration_s=0.5)

    assert len(samples) == 8000
    assert samples[0] == 16000 / 32768.0
    assert samples[-1] == 23999 / 32768.0


def test_a_slice_running_past_the_end_stops_at_the_end(tmp_path):
    pcm = tmp_path / "meeting.pcm"
    write_counting_pcm(pcm, seconds=2.0)

    assert len(read_pcm16(str(pcm), start_s=1.5, duration_s=10.0)) == 8000


@pytest.mark.asyncio
async def test_a_topic_is_diarized_from_its_slice_alone(tmp_path):
    pcm = tmp_path / "meeting.pcm"
    write_counting_pcm(pcm, seconds=2.0)
    diarizer = FakeDiarizer([FakeSegment(0.1, 0.4, 0)])
    transcriber = MeetingTranscriber(
        config=MeetingTranscriberConfig(
            segmentation_model="/unused",
            embedding_model="/unused",
            asr_model_dir="/unused",
        ),
        ten_env=FakeTenEnv(),
        diarize=in_process(lambda _c: diarizer),
        load_extractor=lambda _c: FakeExtractor(),
        load_recogniser=lambda _c: FakeRecogniser(["話題二。"]),
    )

    out = await transcriber.transcribe(
        str(pcm), speakers=2, start_s=1.0, duration_s=0.5
    )

    assert diarizer.sample_counts == [8000]
    assert out[0].start_s == pytest.approx(0.1)  # within the slice


@pytest.mark.asyncio
async def test_a_second_meeting_with_a_different_count_is_clustered_to_it(
    tmp_path,
):
    """One worker, two meetings. The diarizer was built for the first
    meeting's four people; the second has seven, and must not be clustered
    as four."""
    built_for = []

    def loader(config):
        built_for.append(config.speakers)
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
        diarize=in_process(loader),
        load_extractor=lambda _c: FakeExtractor(),
        load_recogniser=lambda _c: FakeRecogniser(["好。", "好。"]),
    )

    await transcriber.transcribe(str(pcm), speakers=4)
    await transcriber.transcribe(str(pcm), speakers=7)

    assert built_for == [4, 7]


def with_extractor(segments, texts, extractor):
    return MeetingTranscriber(
        config=MeetingTranscriberConfig(
            segmentation_model="/unused",
            embedding_model="/unused",
            asr_model_dir="/unused",
        ),
        ten_env=FakeTenEnv(),
        diarize=in_process(lambda _c: FakeDiarizer(segments)),
        load_extractor=lambda _c: extractor,
        load_recogniser=lambda _c: FakeRecogniser(texts),
    )


@pytest.mark.asyncio
async def test_a_turn_of_a_second_or_more_carries_its_voice(tmp_path):
    """Step 3 links speakers across topics by these. A turn under a second
    gives an embedding too noisy to trust, so it carries none."""
    pcm = tmp_path / "t.pcm"
    pcm.write_bytes(b"\x00\x01" * 16000 * 3)
    transcriber = with_extractor(
        [FakeSegment(0.0, 2.0, 0), FakeSegment(2.0, 2.5, 1)],
        ["第一句。", "嗯。"],
        FakeExtractor(vector=(3.0, 4.0)),
    )

    out = await transcriber.transcribe(str(pcm), speakers=2)

    assert out[0].embedding == pytest.approx([0.6, 0.8])  # unit length
    assert out[1].embedding is None


@pytest.mark.asyncio
async def test_a_long_turn_is_embedded_from_its_middle_ten_seconds(tmp_path):
    pcm = tmp_path / "t.pcm"
    # Every sample in second k holds the value k, so a slice names itself.
    np.repeat(np.arange(30, dtype=np.int16), 16000).tofile(pcm)
    extractor = FakeExtractor()
    transcriber = with_extractor(
        [FakeSegment(0.0, 30.0, 0)], ["一段很長的發言。"], extractor
    )

    await transcriber.transcribe(str(pcm), speakers=1)

    fed = extractor.fed[0]
    assert len(fed) == 10 * 16000
    assert fed[0] == 10 / 32768.0  # starts at 10 s
    assert fed[-1] == 19 / 32768.0  # ends inside second 19


def test_the_payload_carries_each_utterance_with_its_voice():
    payload = utterances_payload(
        [
            Utterance(1.0, 2.0, 0, "好。", [0.6, 0.8]),
            Utterance(2.0, 2.5, 1, "嗯。"),
        ]
    )

    assert payload == [
        {
            "start_s": 1.0,
            "end_s": 2.0,
            "speaker": 0,
            "text": "好。",
            "embedding": [0.6, 0.8],
        },
        {
            "start_s": 2.0,
            "end_s": 2.5,
            "speaker": 1,
            "text": "嗯。",
            "embedding": None,
        },
    ]
