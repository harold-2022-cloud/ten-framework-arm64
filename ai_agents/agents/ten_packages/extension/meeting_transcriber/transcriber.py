#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""One recorded topic becomes lines of who said what.

Diarization gives the turns, recognition gives the words, and the join is by
time: each speaker turn is cut out of the samples and recognised on its own.
Both engines are offline and slow enough to matter. Recognition runs on an
executor thread; diarization runs in a child process, because it holds the
GIL for the whole call and would stop every extension's loop (diarize.py).
"""

import asyncio
import glob
import json
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional

import numpy as np

from .config import MeetingTranscriberConfig
from .diarize import SAMPLE_RATE, read_pcm16

DIARIZE_SCRIPT = os.path.join(os.path.dirname(__file__), "diarize.py")


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


def child_python() -> str:
    """The interpreter the runtime embeds, by its versioned name: on the
    board the runtime loads 3.12 while the shell's python3 is 3.13, and
    sherpa-onnx is installed for the former."""
    versioned = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return shutil.which(versioned) or sys.executable or "python3"


async def diarize_in_child(request: dict) -> List[list]:
    """diarize.py in its own process: the turns, or RuntimeError with the
    child's last words."""
    child = await asyncio.create_subprocess_exec(
        child_python(),
        DIARIZE_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await child.communicate(json.dumps(request).encode())
    lines = out.decode(errors="replace").strip().splitlines()
    if child.returncode != 0 or not lines:
        tail = err.decode(errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(
            f"diarization exited {child.returncode}: " + " | ".join(tail)
        )
    return json.loads(lines[-1])["turns"]


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
        diarize: Optional[Callable[[dict], Awaitable[List[list]]]] = None,
        load_recogniser: Optional[Callable[[Any], Any]] = None,
        load_extractor: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self.config = config
        self.ten_env = ten_env
        self._diarize = diarize or diarize_in_child
        self._load_recogniser = load_recogniser or globals()["load_recogniser"]
        self._load_extractor = load_extractor or globals()["load_extractor"]
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
        request = {
            "pcm_path": path,
            "start_s": start_s,
            "duration_s": duration_s,
            "speakers": speakers,
            "cluster_threshold": self.config.cluster_threshold,
            "segmentation_model": self.config.segmentation_model,
            "embedding_model": self.config.embedding_model,
            "num_threads": self.config.num_threads,
        }
        # One segment at a time: both models are hundreds of megabytes and the
        # board has four cores shared with everything else.
        async with self._lock:
            turns = await self._diarize(request)
            return await asyncio.get_running_loop().run_in_executor(
                None, self._recognise, samples, turns
            )

    def _ensure_loaded(self) -> None:
        """Lazily, and only once."""
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

    def _recognise(
        self, samples: np.ndarray, turns: List[list]
    ) -> List[Utterance]:
        # Runs on an executor thread. Recognition gives the GIL back while it
        # decodes (measured: no loop stall over 0.2 s); an embedding holds it
        # for well under a second per turn.
        self._ensure_loaded()
        out: List[Utterance] = []
        for start_s, end_s, speaker in turns:
            begin = int(start_s * SAMPLE_RATE)
            end = min(int(end_s * SAMPLE_RATE), samples.size)
            if end <= begin:
                continue
            stream = self._recogniser.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples[begin:end])
            self._recogniser.decode_stream(stream)
            text = stream.result.text.strip()
            if text:
                out.append(
                    Utterance(
                        start_s=start_s,
                        end_s=end_s,
                        speaker=speaker,
                        text=text,
                        embedding=self._embed(samples, begin, end),
                    )
                )
        return out
