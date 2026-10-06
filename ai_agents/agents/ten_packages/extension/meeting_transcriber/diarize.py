#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Who spoke when in one topic -- run in a child process, not the worker.

sherpa-onnx's OfflineSpeakerDiarization.process() holds the GIL for the
whole call: its binding has no gil_scoped_release. In the worker, even on
an executor thread, that stopped the loop every Python extension shares
for as long as a topic took -- 227 s for a six-minute topic in the dev
container, minutes more on the board -- and with it the uploader's HTTP
answers and the pings that keep the worker from being reaped. In a child
process it holds only its own.

    python3.12 diarize.py < request.json   ->   {"turns": [[start, end, speaker], ...]}

Imports only the standard library and numpy at the top, and nothing from
this package, so it runs as a plain script under the runtime's interpreter.
"""

import json
import sys
from types import SimpleNamespace
from typing import List, Optional

import numpy as np

BYTES_PER_SAMPLE = 2
SAMPLE_RATE = 16000


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


def load_diarizer(config):
    """Imported here so the rest of this file, and its tests, need no models.
    config: anything with the attributes below -- MeetingTranscriberConfig,
    or the request a child is given."""
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


def diarize_slice(request: dict, load=load_diarizer) -> List[list]:
    """[[start_s, end_s, speaker], ...] for one topic's slice, in order.
    Built for this request's head count, so one worker can take meeting
    after meeting of different sizes."""
    samples = read_pcm16(
        request["pcm_path"], request["start_s"], request["duration_s"]
    )
    diarizer = load(SimpleNamespace(**request))
    turns = diarizer.process(samples).sort_by_start_time()
    return [[t.start, t.end, t.speaker] for t in turns]


def _die_with_parent() -> None:
    """A worker stopped mid-topic must not leave a child computing for
    minutes: SIGKILL this process when its parent goes. Linux only."""
    try:
        import ctypes  # pylint: disable=import-outside-toplevel
        import signal  # pylint: disable=import-outside-toplevel

        pr_set_pdeathsig = 1
        ctypes.CDLL("libc.so.6").prctl(pr_set_pdeathsig, signal.SIGKILL)
    except (OSError, AttributeError):
        pass


def main() -> None:
    _die_with_parent()
    request = json.load(sys.stdin)
    turns = diarize_slice(request)
    # The last line of stdout is the answer; anything the engine printed
    # before it is not.
    print(json.dumps({"turns": turns}), flush=True)


if __name__ == "__main__":
    main()
