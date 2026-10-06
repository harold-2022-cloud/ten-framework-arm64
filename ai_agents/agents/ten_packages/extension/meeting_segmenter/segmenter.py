#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A whole recorded meeting becomes topics, measured on the audio's own clock.

Silence ends a topic. The whole file is already on disk when this runs, so
"how long was it quiet" is a count of samples, never the wall clock -- an
hour of meeting arrives over the network in seconds.
"""

import os
from typing import Callable, List, Tuple

import numpy as np
import soundfile as sf

from .config import MeetingSegmenterConfig

Span = Tuple[float, float]

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
DECODE_BLOCK = SAMPLE_RATE * 10


def decode(ogg_path: str, pcm_path: str) -> int:
    """The uploaded Ogg-Opus becomes raw PCM16 on disk; returns samples.

    Streamed in blocks, so memory does not grow with the meeting. Anything
    but 16 kHz mono is refused by name: the uploader already checked, and a
    file that slipped past it should fail here, said plainly, rather than
    reach the models at the wrong rate.
    """
    info = sf.info(ogg_path)
    if info.samplerate != SAMPLE_RATE:
        raise ValueError(
            f"{ogg_path} is {info.samplerate} Hz; the models want "
            f"{SAMPLE_RATE}"
        )
    if info.channels != 1:
        raise ValueError(f"{ogg_path} has {info.channels} channels; mono only")
    samples = 0
    with open(pcm_path, "wb") as pcm:
        for block in sf.blocks(ogg_path, blocksize=DECODE_BLOCK, dtype="int16"):
            pcm.write(block.tobytes())
            samples += len(block)
    return samples


def scan(pcm_path: str, vad) -> List[Span]:
    """Where the speech is, in seconds of the file.

    The VAD is fed PCM16 from disk one window at a time, as float in
    [-1, 1]. A window's place in the file is its index times its length --
    the sample cursor is the clock. A trailing part-window is left out: the
    model takes exactly window_size() samples, and it is under 16 ms.
    """
    window = vad.window_size()
    runs: List[Tuple[int, int]] = []
    with open(pcm_path, "rb") as pcm:
        index = 0
        while True:
            raw = pcm.read(window * BYTES_PER_SAMPLE)
            if len(raw) < window * BYTES_PER_SAMPLE:
                break
            samples = np.frombuffer(raw, dtype=np.int16)
            if vad.is_speech(samples.astype(np.float32) / 32768.0):
                if runs and runs[-1][1] == index:
                    runs[-1] = (runs[-1][0], index + 1)
                else:
                    runs.append((index, index + 1))
            index += 1
    return [
        (a * window / SAMPLE_RATE, b * window / SAMPLE_RATE) for a, b in runs
    ]


def cut(
    runs: List[Span],
    total_s: float,
    silence_s: float,
    min_s: float,
    max_s: float,
) -> List[Span]:
    """Speech runs, in seconds of the file, become topics that tile it.

    Every second of the recording lands in exactly one topic: boundaries go
    in the middle of each silence of silence_s or more, the first topic
    starts at 0 and the last ends at total_s. Nothing is dropped on the
    VAD's say-so -- on far-field audio it misses quiet speakers, and a topic
    trimmed to what it heard would take their words out of the transcript.

    A topic longer than max_s is cut anyway: a meeting with no break has no
    silence to cut on, and the longest topic sets the memory ceiling. A
    topic holding less than min_s of speech joins a neighbour.
    """
    if not runs:
        return []
    groups: List[List[Span]] = [[runs[0]]]
    for start, end in runs[1:]:
        if start - groups[-1][-1][1] >= silence_s:
            groups.append([(start, end)])
        else:
            groups[-1].append((start, end))
    edges = (
        [0.0]
        + [
            (prev[-1][1] + nxt[0][0]) / 2
            for prev, nxt in zip(groups, groups[1:])
        ]
        + [total_s]
    )
    topics: List[Tuple[float, float, float]] = []
    for (start, end), group in zip(zip(edges, edges[1:]), groups):
        topics.extend(_cut_long(start, end, group, min_s, max_s))
    return _merge_short(topics, min_s, max_s)


def _speech_in(runs: List[Span], start: float, end: float) -> float:
    return sum(max(0.0, min(e, end) - max(s, start)) for s, e in runs)


def _longest_pause(runs: List[Span], low: float, high: float):
    """The middle of the longest pause whose middle lies in [low, high]."""
    at, longest = None, -1.0
    for prev, nxt in zip(runs, runs[1:]):
        middle = (prev[1] + nxt[0]) / 2
        if low <= middle <= high and nxt[0] - prev[1] > longest:
            at, longest = middle, nxt[0] - prev[1]
    return at


def _cut_long(
    start: float, end: float, runs: List[Span], min_s: float, max_s: float
) -> List[Tuple[float, float, float]]:
    """Cut a stretch longer than max_s, which is only ever a cut for memory.

    In the middle of the longest pause in the second half of the allowed
    window, so the piece stays near the ceiling; failing that, the longest
    pause anywhere past min_s, since a short topic beats cutting a speaker
    off mid-sentence; failing that, exactly at max_s. Pieces carry their
    seconds of speech."""
    out: List[Tuple[float, float, float]] = []
    while end - start > max_s:
        limit = start + max_s
        at = _longest_pause(runs, start + max_s / 2, limit)
        if at is None:
            at = _longest_pause(runs, start + min_s, limit)
        if at is None:
            at = limit
        out.append((start, at, _speech_in(runs, start, at)))
        runs = [(max(s, at), e) for s, e in runs if e > at]
        start = at
    out.append((start, end, _speech_in(runs, start, end)))
    return out


def _merge_short(
    topics: List[Tuple[float, float, float]], min_s: float, max_s: float
) -> List[Span]:
    """A topic with too little speech to summarise joins the next one, or
    the previous one when it is last -- unless the join would breach max_s,
    the memory ceiling; then it stands alone."""
    topics = list(topics)
    out: List[Tuple[float, float, float]] = []
    for i, (start, end, speech) in enumerate(topics):
        short = speech < min_s
        if short and i + 1 < len(topics) and topics[i + 1][1] - start <= max_s:
            _, nxt_end, nxt_speech = topics[i + 1]
            topics[i + 1] = (start, nxt_end, speech + nxt_speech)
        elif short and out and end - out[-1][0] <= max_s:
            prev_start, _, prev_speech = out[-1]
            out[-1] = (prev_start, end, prev_speech + speech)
        else:
            out.append((start, end, speech))
    return [(start, end) for start, end, _ in out]


def load_vad(config: MeetingSegmenterConfig):
    """sherpa-onnx's TEN VAD: 256-sample (16 ms) windows, one bool each.

    Imported here so the rest of this file, and its tests, need no model.
    A fresh one per meeting -- the model carries state from window to
    window, and a meeting should not start in the last one's.
    """
    import sherpa_onnx  # pylint: disable=import-outside-toplevel

    if not os.path.isfile(config.vad_model):
        raise ValueError(f"vad_model is not a file: {config.vad_model!r}")
    vad_config = sherpa_onnx.VadModelConfig(
        ten_vad=sherpa_onnx.TenVadModelConfig(
            model=config.vad_model, threshold=config.vad_threshold
        ),
        sample_rate=SAMPLE_RATE,
        num_threads=1,
        provider="cpu",
    )
    if not vad_config.validate():
        raise ValueError(
            f"sherpa-onnx rejected the VAD config for {config.vad_model!r}"
        )
    return sherpa_onnx.VadModel.create(vad_config)


def segment_file(
    ogg_path: str,
    work_dir: str,
    config: MeetingSegmenterConfig,
    make_vad: Callable[[], object],
) -> dict:
    """The whole step, as the payload segments_ready carries.

    A failure is reported in "error", never raised: the controller turns it
    into a failed meeting with its original audio kept, and the worker
    stays up for the next upload.
    """
    pcm_path = os.path.join(work_dir, "audio.pcm")
    try:
        samples = decode(ogg_path, pcm_path)
        topics = cut(
            scan(pcm_path, make_vad()),
            total_s=samples / SAMPLE_RATE,
            silence_s=config.segment_silence_s,
            min_s=config.min_segment_s,
            max_s=config.max_segment_s,
        )
    except Exception as failure:  # pylint: disable=broad-except
        return {
            "pcm_path": pcm_path,
            "duration_s": 0.0,
            "segments": [],
            "error": f"{type(failure).__name__}: {failure}",
        }
    return {
        "pcm_path": pcm_path,
        "duration_s": samples / SAMPLE_RATE,
        "segments": [
            {"id": f"t{n:02d}", "start_s": start, "end_s": end}
            for n, (start, end) in enumerate(topics, 1)
        ],
        "error": None,
    }
