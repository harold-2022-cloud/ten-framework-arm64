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


def read_pcm16(path: str) -> np.ndarray:
    """The recorder's own format: mono, 16 kHz, no header."""
    raw = open(path, "rb").read()
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
    ) -> None:
        self.config = config
        self.ten_env = ten_env
        self._load_diarizer = load_diarizer or globals()["load_diarizer"]
        self._load_recogniser = load_recogniser or globals()["load_recogniser"]
        self._diarizer = None
        self._recogniser = None
        self._lock = asyncio.Lock()

    async def transcribe(self, path: str, speakers: int) -> List[Utterance]:
        samples = read_pcm16(path)
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
        if self._diarizer is None:
            config = self.config.model_copy(update={"speakers": speakers})
            self._diarizer = self._load_diarizer(config)
        if self._recogniser is None:
            self._recogniser = self._load_recogniser(self.config)

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
                    )
                )
        return out
