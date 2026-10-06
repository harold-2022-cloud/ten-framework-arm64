#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""One recorded topic becomes lines of who said what.

Diarization gives the turns, recognition gives the words, and the join is by
time: each speaker turn is cut out of the samples and recognised on its own.
Both engines are offline and both are slow enough to matter, so they run in an
executor and the loop keeps going.
"""

import asyncio
import glob
import os
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

import numpy as np

from .config import MeetingTranscriberConfig

BYTES_PER_SAMPLE = 2
SAMPLE_RATE = 16000


@dataclass
class Utterance:
    """One speaker turn, placed in the segment and transcribed."""

    start_s: float
    end_s: float
    speaker: int
    text: str
    # Unit-length voice embedding for linking speakers across topics; None
    # for a turn too short to give a trustworthy one.
    embedding: Optional[List[float]] = None


def utterances_payload(utterances: List[Utterance]) -> List[dict]:
    """The utterances as segment_transcribed carries them."""
    return [
        {
            "start_s": u.start_s,
            "end_s": u.end_s,
            "speaker": u.speaker,
            "text": u.text,
            "embedding": u.embedding,
        }
        for u in utterances
    ]


def read_pcm16(
    path: str, start_s: float = 0.0, duration_s: Optional[float] = None
) -> np.ndarray:
    """The recorder's own format: mono, 16 kHz, no header.

    A slice reads only its own bytes -- one topic out of an hour's PCM, not
    the hour. Without start_s and duration_s it reads the whole file.
    """
    with open(path, "rb") as pcm:
        pcm.seek(int(round(start_s * SAMPLE_RATE)) * BYTES_PER_SAMPLE)
        if duration_s is None:
            raw = pcm.read()
        else:
            raw = pcm.read(
                int(round(duration_s * SAMPLE_RATE)) * BYTES_PER_SAMPLE
            )
    raw = raw[: len(raw) - (len(raw) % BYTES_PER_SAMPLE)]
    samples = np.frombuffer(raw, dtype=np.int16)
    return (samples.astype(np.float32) / 32768.0).copy()


def load_diarizer(config: MeetingTranscriberConfig):
    """Imported here so the rest of this file, and its tests, need no models."""
    import sherpa_onnx  # pylint: disable=import-outside-toplevel

    return sherpa_onnx.OfflineSpeakerDiarization(
        sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=config.segmentation_model
                ),
                num_threads=config.num_threads,
                provider="cpu",
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=config.embedding_model,
                num_threads=config.num_threads,
                provider="cpu",
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=config.speakers,
                threshold=config.cluster_threshold,
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
    )


def load_extractor(config: MeetingTranscriberConfig):
    """The diarizer's own embedding model, loaded again on its own: the
    diarizer keeps its embeddings internal, and step 3 needs one per turn."""
    import sherpa_onnx  # pylint: disable=import-outside-toplevel

    return sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=config.embedding_model,
            num_threads=config.num_threads,
            provider="cpu",
        )
    )


def load_recogniser(config: MeetingTranscriberConfig):
    import sherpa_onnx  # pylint: disable=import-outside-toplevel

    model = sorted(glob.glob(os.path.join(config.asr_model_dir, "model*.onnx")))
    if not model:
        raise FileNotFoundError(f"no model*.onnx in {config.asr_model_dir}")
    tokens = os.path.join(config.asr_model_dir, "tokens.txt")
    if not os.path.isfile(tokens):
        raise FileNotFoundError(f"no tokens.txt in {config.asr_model_dir}")
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=model[0],
        tokens=tokens,
        num_threads=config.num_threads,
        use_itn=True,  # "three days" rather than "three days" spelled out
        provider="cpu",
    )


class MeetingTranscriber:
    """Drives both engines for one segment at a time."""

    def __init__(
        self,
        config: MeetingTranscriberConfig,
        ten_env,
        load_diarizer: Optional[Callable[[Any], Any]] = None,
        load_recogniser: Optional[Callable[[Any], Any]] = None,
        load_extractor: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self.config = config
        self.ten_env = ten_env
        self._load_diarizer = load_diarizer or globals()["load_diarizer"]
        self._load_recogniser = load_recogniser or globals()["load_recogniser"]
        self._load_extractor = load_extractor or globals()["load_extractor"]
        self._diarizer = None
        self._diarizer_speakers: Optional[int] = None
        self._recogniser = None
        self._extractor = None
        self._lock = asyncio.Lock()

    async def transcribe(
        self,
        path: str,
        speakers: int,
        start_s: float = 0.0,
        duration_s: Optional[float] = None,
    ) -> List[Utterance]:
        """Who said what in one topic. Utterance times are offsets within
        the slice; the caller adds the topic's own start_s."""
        samples = read_pcm16(path, start_s, duration_s)
        if samples.size == 0:
            return []
        # One segment at a time: both models are hundreds of megabytes and the
        # board has four cores shared with everything else.
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._work, samples, speakers
            )

    def _ensure_loaded(self, speakers: int) -> None:
        """Lazily, and only once: 47 MB has no business being resident for a
        whole meeting when it is wanted for a minute of it."""
        # Rebuilt when the count changes: one worker serves meeting after
        # meeting, and a diarizer built for the first one's four people would
        # cluster the next one's seven as four.
        if self._diarizer is None or self._diarizer_speakers != speakers:
            config = self.config.model_copy(update={"speakers": speakers})
            self._diarizer = self._load_diarizer(config)
            self._diarizer_speakers = speakers
        if self._recogniser is None:
            self._recogniser = self._load_recogniser(self.config)
        if self._extractor is None:
            self._extractor = self._load_extractor(self.config)

    def _embed(self, samples: np.ndarray, begin: int, end: int):
        """One turn's voice, from its middle embed_max_turn_s at most, scaled
        to unit length; None when the turn is under embed_min_turn_s."""
        if (end - begin) / SAMPLE_RATE < self.config.embed_min_turn_s:
            return None
        keep = min(end - begin, int(self.config.embed_max_turn_s * SAMPLE_RATE))
        start = begin + (end - begin - keep) // 2
        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples[start : start + keep])
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            return None
        vector = np.asarray(self._extractor.compute(stream), dtype=np.float64)
        norm = float(np.linalg.norm(vector))
        return (vector / norm).tolist() if norm > 0 else None

    def _work(self, samples: np.ndarray, speakers: int) -> List[Utterance]:
        # Runs on a worker thread. Both calls are C++ holding it for seconds.
        self._ensure_loaded(speakers)
        turns = self._diarizer.process(samples).sort_by_start_time()

        out: List[Utterance] = []
        for turn in turns:
            begin = int(turn.start * SAMPLE_RATE)
            end = min(int(turn.end * SAMPLE_RATE), samples.size)
            if end <= begin:
                continue
            stream = self._recogniser.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples[begin:end])
            self._recogniser.decode_stream(stream)
            text = stream.result.text.strip()
            if text:
                out.append(
                    Utterance(
                        start_s=turn.start,
                        end_s=turn.end,
                        speaker=turn.speaker,
                        text=text,
                        embedding=self._embed(samples, begin, end),
                    )
                )
        return out
