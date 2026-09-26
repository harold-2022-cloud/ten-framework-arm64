#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Can this board separate speakers, and what does it cost?

A throwaway measurement, not a component. sherpa-onnx 1.13.8 -- the version
already on the board for ASR and TTS -- ships OfflineSpeakerDiarization, but
nobody has run it on an N1-655. This answers three things before any of it is
designed around: does it run, how long does it take against the length of the
recording, and how much memory does it hold at once.

Diarization here is offline: it wants the whole recording, not a stream. For a
meeting that is recorded and summarised afterwards, that is the right shape --
and it is why this probe takes a file rather than a microphone.

  python3 tools/ambarella/probe_diarization.py --fetch
  python3 tools/ambarella/probe_diarization.py --audio /tmp/meeting.pcm
  python3 tools/ambarella/probe_diarization.py --audio talk.wav --speakers 3

Audio may be a .wav, or the raw PCM16 the ASR extension's dump writes: mono,
16 kHz, no header. Reads and measures; it writes nothing but the models it is
asked to fetch.

Tell it how many people are in the room when you know. Measured in the x86
container against sherpa's own four-speaker recording: the default threshold
found seven, 0.7 found five, and --speakers 4 found four with segments in the
right places. Clustering without a count over-splits one person into several,
which in a transcript reads as a room full of strangers.
"""

import argparse
import os
import resource
import subprocess
import sys
import tarfile
import time
import wave

SEG_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
# The release tag really is spelled "recongition" upstream.
EMB_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/"
    "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
)
SEG_MODEL = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
EMB_MODEL = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
SAMPLE_RATE = 16000


def say(msg):
    print(f"\n\033[1m===== {msg}\033[0m")


def ok(msg):
    print(f"  \033[32mok\033[0m    {msg}")


def info(msg):
    print(f"        {msg}")


def die(msg):
    print(f"\nFATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def fetch(root):
    """Pull both models. Separate from the measurement so a board without a
    route out can be given them by hand and still run the rest."""
    os.makedirs(root, exist_ok=True)
    seg_dir = os.path.join(root, "sherpa-onnx-pyannote-segmentation-3-0")
    if not os.path.isdir(seg_dir):
        tar = os.path.join(root, os.path.basename(SEG_URL))
        info(f"fetching {os.path.basename(SEG_URL)}")
        subprocess.run(["curl", "-fL", "-o", tar, SEG_URL], check=True)
        # tarfile rather than `tar xjf`: bzip2 is not on every image, and the
        # failure when it is missing looks like a corrupt download.
        with tarfile.open(tar, "r:bz2") as archive:
            archive.extractall(root)
        os.remove(tar)
    ok(f"segmentation model in {seg_dir}")

    emb = os.path.join(root, EMB_MODEL)
    if not os.path.isfile(emb):
        info(f"fetching {EMB_MODEL}")
        subprocess.run(["curl", "-fL", "-o", emb, EMB_URL], check=True)
    ok(f"embedding model {os.path.getsize(emb) / 1e6:.0f} MB")


def read_audio(path):
    """Float32 in [-1, 1], whatever the container.

    The raw branch is the ASR extension's dump format on purpose: that is the
    recording a meeting would already have, and its byte offsets line up with
    the timestamps on the transcripts.
    """
    import numpy as np

    if path.lower().endswith(".wav"):
        with wave.open(path, "rb") as wav:
            if wav.getnchannels() != 1:
                die(f"{path} has {wav.getnchannels()} channels; mono only")
            if wav.getsampwidth() != 2:
                die(f"{path} is not 16-bit")
            rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())
        if rate != SAMPLE_RATE:
            die(f"{path} is {rate} Hz; the models want {SAMPLE_RATE}")
    else:
        raw = open(path, "rb").read()
        raw = raw[: len(raw) - (len(raw) % 2)]

    samples = np.frombuffer(raw, dtype=np.int16)
    return (samples.astype(np.float32) / 32768.0).copy()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--models", default=os.path.expanduser("~/diarization_models"))
    p.add_argument("--audio")
    p.add_argument("--fetch", action="store_true", help="download the models and stop")
    p.add_argument("--threads", type=int, default=2)
    p.add_argument(
        "--speakers",
        type=int,
        default=-1,
        help="how many people, when you know; -1 clusters by threshold",
    )
    p.add_argument("--threshold", type=float, default=0.5)
    args = p.parse_args()

    say("1. Models")
    if args.fetch:
        fetch(args.models)
        info("now run again with --audio")
        return 0
    seg = os.path.join(args.models, SEG_MODEL)
    emb = os.path.join(args.models, EMB_MODEL)
    for path in (seg, emb):
        if not os.path.isfile(path):
            die(f"missing {path}\n       Run with --fetch, or pass --models.")
    ok(f"{seg}")
    ok(f"{emb}")

    if not args.audio:
        die("no --audio. A .wav at 16 kHz mono, or the ASR dump's raw PCM16.")

    say("2. Audio")
    started = time.monotonic()
    samples = read_audio(args.audio)
    seconds = len(samples) / SAMPLE_RATE
    ok(f"{args.audio}")
    info(f"{seconds:.1f} s ({seconds / 60:.1f} min), {len(samples)} samples")
    info(f"read in {time.monotonic() - started:.1f} s")

    say("3. Loading")
    import sherpa_onnx

    started = time.monotonic()
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=seg
            ),
            num_threads=args.threads,
            provider="cpu",
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=emb, num_threads=args.threads, provider="cpu"
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=args.speakers, threshold=args.threshold
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        die("sherpa-onnx rejected the configuration")
    diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
    load_s = time.monotonic() - started
    ok(f"loaded in {load_s:.1f} s, {args.threads} threads")
    if diarizer.sample_rate != SAMPLE_RATE:
        die(f"the models want {diarizer.sample_rate} Hz, the audio is {SAMPLE_RATE}")

    say("4. Diarizing")
    last = [0]

    def progress(done, total):
        pct = 100 * done // max(1, total)
        if pct >= last[0] + 20:
            last[0] = pct
            print(f"        {pct}%  ({time.monotonic() - started:.0f} s)")
        return 0

    started = time.monotonic()
    result = diarizer.process(samples, callback=progress)
    elapsed = time.monotonic() - started
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    say("5. What it cost")
    info(f"audio           {seconds:.1f} s")
    info(f"diarization     {elapsed:.1f} s")
    info(f"real-time factor {elapsed / seconds:.2f}   "
         f"(a {10:.0f} min meeting -> {elapsed / seconds * 600 / 60:.1f} min)")
    info(f"peak memory     {peak_mb:.0f} MB, including the load")

    say("6. What it heard")
    segments = result.sort_by_start_time()
    info(f"{result.num_speakers} speakers, {result.num_segments} segments")
    print()
    for seg_ in segments[:40]:
        print(
            f"    {seg_.start:7.2f} - {seg_.end:7.2f}  "
            f"({seg_.duration:5.2f} s)  speaker {seg_.speaker}"
        )
    if len(segments) > 40:
        print(f"    ... {len(segments) - 40} more")

    print()
    print("  A plausible speaker count and segments that line up with who was")
    print("  talking means this is worth designing around. A real-time factor")
    print("  near or above 1 means a meeting takes as long to process as it")
    print("  took to hold, which is still fine for something run after it ends.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
