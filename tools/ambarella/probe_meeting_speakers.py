#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Can the board tell five to ten people apart, and keep them apart all meeting?

A measurement, not a component. probe_diarization.py answered "does it run";
this answers what the meeting design needs before it is built:

  A. The design as written diarizes each topic on its own and writes the
     cluster number straight into the record. Cluster numbers are given per
     call, so "speaker 2" in topic 1 and "speaker 2" in topic 3 need not be the
     same person. How wrong does that make a real meeting?
  B. The fix that keeps memory bounded by the longest topic: diarize per
     topic, take one embedding per turn with the model diarization already
     loads, and cluster every turn of the meeting together. No enrollment,
     no names -- only "the same voice gets the same number".
  C. The alternative that needs no linking: diarize the whole meeting in one
     call. Consistent by construction; what does it cost on the board?

Scored against a reference RTTM (AISHELL-4 ships one per session), so the
answer is a number, not an impression:

  DER        diarization error rate, 0.25 s collar, overlap scored
  confusion  the part of DER that is "right time, wrong person" -- the one
             that says whether people are told apart
  consistent of the people who speak in two or more topics, how many keep one
             number throughout

  python3 probe_meeting_speakers.py --check --audio m.wav --rttm m.rttm
  python3 probe_meeting_speakers.py --audio m.wav --rttm m.rttm
  python3 probe_meeting_speakers.py --audio m.wav --rttm m.rttm --minutes 8

Audio is a 16 kHz mono 16-bit .wav. Results go to --out as JSON, rewritten
after every stage, so a run cut short still leaves what it finished.
"""

import argparse
import bisect
import glob
import json
import os
import resource
import sys
import time
import wave

SAMPLE_RATE = 16000
SEG_MODEL = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
EMB_MODEL = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"


def say(msg):
    print(f"\n\033[1m===== {msg}\033[0m", flush=True)


def ok(msg):
    print(f"  \033[32mok\033[0m    {msg}", flush=True)


def info(msg):
    print(f"        {msg}", flush=True)


def warn(msg):
    print(f"  \033[33mwarn\033[0m  {msg}", flush=True)


def die(msg):
    print(f"\nFATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def peak_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def mmss(seconds):
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


# --------------------------------------------------------------------------
# Inputs


def read_wav(path):
    import numpy as np

    with wave.open(path, "rb") as wav:
        if wav.getnchannels() != 1:
            die(f"{path} has {wav.getnchannels()} channels; mono only")
        if wav.getsampwidth() != 2:
            die(f"{path} is not 16-bit")
        if wav.getframerate() != SAMPLE_RATE:
            die(f"{path} is {wav.getframerate()} Hz; the models want 16000")
        raw = wav.readframes(wav.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def read_rttm(path):
    """[(start, end, speaker)] from the SPEAKER lines."""
    turns = []
    for line in open(path, encoding="utf-8"):
        f = line.split()
        if len(f) >= 8 and f[0] == "SPEAKER":
            start, dur = float(f[3]), float(f[4])
            turns.append((start, start + dur, f[7]))
    return sorted(turns)


def clip(turns, a, b):
    """The part of each turn inside [a, b)."""
    return [(max(s, a), min(e, b), k) for s, e, k in turns if e > a and s < b]


def speech_by(turns):
    out = {}
    for s, e, k in turns:
        out[k] = out.get(k, 0.0) + (e - s)
    return out


# --------------------------------------------------------------------------
# Scoring


def hungarian_max(weight):
    """One-to-one rows -> columns maximising total weight, O(n^3).

    Written out rather than imported: scipy is not on the board. The classic
    potentials formulation on a square matrix padded with zeros, so a row or
    column left unmatched simply scores nothing.
    """
    n_r = len(weight)
    n_c = len(weight[0]) if n_r else 0
    n = max(n_r, n_c)
    cost = [[0.0] * n for _ in range(n)]
    for i in range(n_r):
        for j in range(n_c):
            cost[i][j] = -float(weight[i][j])
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return {p[j] - 1: j - 1 for j in range(1, n + 1)
            if 0 < p[j] <= n_r and j <= n_c}


def frames(turns, labels, n, frame):
    """Boolean matrix label x frame."""
    import numpy as np

    m = np.zeros((len(labels), n), dtype=bool)
    index = {k: i for i, k in enumerate(labels)}
    for s, e, k in turns:
        a, b = int(round(s / frame)), int(round(e / frame))
        m[index[k], max(0, a):min(n, b)] = True
    return m


def score(ref, hyp, total_s, frame=0.01, collar=0.25):
    """NIST-style DER with an optimal one-to-one speaker mapping."""
    import numpy as np

    n = int(np.ceil(total_s / frame))
    ref_labels = sorted({k for _, _, k in ref})
    hyp_labels = sorted({k for _, _, k in hyp}, key=str)
    r = frames(ref, ref_labels, n, frame)
    h = frames(hyp, hyp_labels, n, frame) if hyp_labels else np.zeros((0, n), bool)

    scored = np.ones(n, dtype=bool)
    c = int(round(collar / frame))
    for s, e, _ in ref:
        for edge in (s, e):
            x = int(round(edge / frame))
            scored[max(0, x - c):min(n, x + c)] = False

    r, h = r[:, scored], h[:, scored]
    n_ref, n_hyp = r.sum(0), h.sum(0)
    overlap = (r[:, None, :] & h[None, :, :]).sum(2) if len(hyp_labels) else None
    mapping = hungarian_max(overlap.tolist()) if overlap is not None else {}
    correct = sum(int(overlap[i, j]) for i, j in mapping.items())

    total = int(n_ref.sum())
    miss = int(np.maximum(0, n_ref - n_hyp).sum())
    fa = int(np.maximum(0, n_hyp - n_ref).sum())
    conf = int(np.minimum(n_ref, n_hyp).sum()) - correct
    pct = lambda x: round(100.0 * x / max(1, total), 2)  # noqa: E731
    return {
        "der": pct(miss + fa + conf),
        "miss": pct(miss),
        "false_alarm": pct(fa),
        "confusion": pct(conf),
        "speakers_ref": len(ref_labels),
        "speakers_found": len(hyp_labels),
        "mapping": {ref_labels[i]: str(hyp_labels[j]) for i, j in mapping.items()},
    }


def consistency(ref, hyp, topics, min_s=10.0):
    """For each reference speaker, the hypothesis label that holds most of
    their speech in each topic they speak in for at least min_s. Consistent
    when that label never changes."""
    rows, kept = {}, 0
    for k in sorted({k for _, _, k in ref}):
        seq = []
        for a, b in topics:
            mine = [(s, e) for s, e, kk in clip(ref, a, b) if kk == k]
            if sum(e - s for s, e in mine) < min_s:
                seq.append(None)
                continue
            votes = {}
            for s, e, label in clip(hyp, a, b):
                for ms, me in mine:
                    o = min(e, me) - max(s, ms)
                    if o > 0:
                        votes[label] = votes.get(label, 0.0) + o
            seq.append(str(max(votes, key=votes.get)) if votes else "-")
        present = [x for x in seq if x is not None]
        rows[k] = seq
        if len(present) >= 2 and len(set(present)) == 1:
            kept += 1
    eligible = sum(1 for s in rows.values()
                   if len([x for x in s if x is not None]) >= 2)
    return {"consistent": kept, "eligible": eligible, "per_speaker": rows}


# --------------------------------------------------------------------------
# Engines


def make_diarizer(args, speakers):
    import sherpa_onnx

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=os.path.join(args.models, SEG_MODEL)
            ),
            num_threads=args.threads,
            provider="cpu",
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=os.path.join(args.models, EMB_MODEL),
            num_threads=args.threads,
            provider="cpu",
        ),
        # The same settings meeting_transcriber uses, so what is measured
        # here is what the graph would do.
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=speakers, threshold=args.threshold
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        die("sherpa-onnx rejected the diarization configuration")
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def diarize(diarizer, samples, offset, label_progress=False):
    started = time.monotonic()
    last = [0]

    def progress(done, total):
        pct = 100 * done // max(1, total)
        if label_progress and pct >= last[0] + 10:
            last[0] = pct
            info(f"  {pct:3d}%  {time.monotonic() - started:6.0f} s")
        return 0

    result = diarizer.process(samples, callback=progress)
    return [
        (offset + s.start, offset + s.end, s.speaker)
        for s in result.sort_by_start_time()
    ]


def make_extractor(args):
    import sherpa_onnx

    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=os.path.join(args.models, EMB_MODEL),
        num_threads=args.threads,
        provider="cpu",
    )
    if not config.validate():
        die("sherpa-onnx rejected the embedding configuration")
    return sherpa_onnx.SpeakerEmbeddingExtractor(config)


def embed(extractor, samples):
    import numpy as np

    stream = extractor.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    stream.input_finished()
    if not extractor.is_ready(stream):
        return None
    v = np.asarray(extractor.compute(stream), dtype=np.float32)
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else None


def average_linkage(vectors, weights, n_clusters, threshold=0.0):
    """Weighted average linkage on cosine similarity: merge until n_clusters
    remain, or with n_clusters <= 0 until the best merge falls below
    threshold. A running matrix of summed pair similarities makes each merge
    one vectorised pass rather than a loop over every pair of clusters."""
    import numpy as np

    x = np.asarray(vectors, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    pair_sum = (x @ x.T) * w[:, None] * w[None, :]
    np.fill_diagonal(pair_sum, -np.inf)
    size = w.copy()
    alive = np.ones(len(x), dtype=bool)
    members = [[i] for i in range(len(x))]
    while alive.sum() > 1:
        if n_clusters > 0 and alive.sum() <= n_clusters:
            break
        avg = pair_sum / (size[:, None] * size[None, :])
        avg[~alive, :] = -np.inf
        avg[:, ~alive] = -np.inf
        i, j = np.unravel_index(int(np.argmax(avg)), avg.shape)
        if n_clusters <= 0 and avg[i, j] < threshold:
            break
        pair_sum[i, :] += pair_sum[j, :]
        pair_sum[:, i] += pair_sum[:, j]
        pair_sum[i, i] = -np.inf
        size[i] += size[j]
        alive[j] = False
        members[i] += members[j]
        members[j] = []
    return [m for m in members if m]


def link_turns(extractor, samples, per_topic, speakers, args):
    """Design B: one embedding per turn, every turn of the meeting clustered
    together, and each turn takes its cluster's number.

    Turns are the unit, not (topic, cluster) pairs. Forcing a topic where two
    people spoke into the meeting's six clusters makes fragments, and a
    fragment's mean embedding is noise. Clustering straight to the speaker
    count then lets noisy items hold clusters of their own to the end, and
    real people are merged instead -- measured on AISHELL-4 M_R003S01C01:
    28 % confusion. So: over-cluster to over_factor x the count, keep the
    `speakers` clusters holding the most speech, and fold every other cluster
    into the nearest kept one.
    """
    import numpy as np

    turns = [(s, e, k, t) for t, segs in enumerate(per_topic)
             for s, e, k in segs]
    vecs, wts, owner = [], [], []
    for n, (s, e, _, _) in enumerate(turns):
        if e - s < args.min_turn_s:
            continue
        mid, half = (s + e) / 2, min(e - s, args.max_turn_s) / 2
        v = embed(extractor, samples[int((mid - half) * SAMPLE_RATE):
                                     int((mid + half) * SAMPLE_RATE)])
        if v is not None:
            vecs.append(v)
            wts.append(2 * half)
            owner.append(n)
    if not vecs:
        return [(s, e, 0) for s, e, _, _ in turns], 0, 0.0

    x, w = np.asarray(vecs), np.asarray(wts)
    if speakers > 0:
        groups = average_linkage(x, w, speakers * args.over_factor)
        groups.sort(key=lambda m: -w[m].sum())
        keep, rest = groups[:speakers], groups[speakers:]
    else:
        keep, rest = average_linkage(x, w, -1, args.link_threshold), []

    def centroid(m):
        c = (x[m] * w[m, None]).sum(0)
        return c / np.linalg.norm(c)

    cents = np.asarray([centroid(m) for m in keep])
    label = {}
    for g, m in enumerate(keep):
        for i in m:
            label[owner[i]] = g
    for m in rest:
        g = int(np.argmax(cents @ centroid(m)))
        for i in m:
            label[owner[i]] = g

    # Turns too short to embed: the number most of their own (topic, cluster)
    # got; failing that, the nearest labelled turn in time.
    votes = {}
    for n, g in label.items():
        s, e, k, t = turns[n]
        bucket = votes.setdefault((t, k), {})
        bucket[g] = bucket.get(g, 0.0) + (e - s)
    labelled = sorted((turns[n][0], g) for n, g in label.items())
    starts = [s for s, _ in labelled]
    hyp = []
    for n, (s, e, k, t) in enumerate(turns):
        g = label.get(n)
        if g is None:
            v = votes.get((t, k))
            if v:
                g = max(v, key=v.get)
            else:
                j = bisect.bisect_left(starts, s)
                near = [labelled[c] for c in (j - 1, j) if 0 <= c < len(labelled)]
                g = min(near, key=lambda c: abs(c[0] - s))[1]
        hyp.append((s, e, g))
    return hyp, len(vecs), float(w.sum())


# --------------------------------------------------------------------------
# Run


def preflight(args):
    say("0. Preflight")
    ok(f"python {sys.version.split()[0]}, {os.cpu_count()} cores, "
       f"--threads {args.threads}")
    try:
        meminfo = dict(
            (l.split(":")[0], int(l.split()[1]))
            for l in open("/proc/meminfo") if ":" in l)
        ok(f"memory available {meminfo.get('MemAvailable', 0) // 1024} MB")
    except OSError:
        warn("no /proc/meminfo")
    try:
        import numpy as np
        ok(f"numpy {np.__version__}")
    except ImportError:
        die("numpy is not importable by this python3")
    try:
        import sherpa_onnx
    except ImportError:
        die("sherpa_onnx is not importable by this python3")
    missing = [n for n in ("OfflineSpeakerDiarization", "SpeakerEmbeddingExtractor",
                           "OfflineRecognizer") if not hasattr(sherpa_onnx, n)]
    if missing:
        die(f"sherpa_onnx {sherpa_onnx.__version__} lacks {missing}")
    ok(f"sherpa_onnx {sherpa_onnx.__version__}")

    for name in (SEG_MODEL, EMB_MODEL):
        path = os.path.join(args.models, name)
        if not os.path.isfile(path):
            die(f"missing {path}\n       Run tools/ambarella/install_meeting_models.sh")
        ok(path)
    if args.asr_seconds > 0:
        models = sorted(glob.glob(os.path.join(args.asr_model_dir, "model*.onnx")))
        tokens = os.path.join(args.asr_model_dir, "tokens.txt")
        if not models or not os.path.isfile(tokens):
            die(f"no SenseVoice model*.onnx + tokens.txt in {args.asr_model_dir}\n"
                "       Run tools/ambarella/install_meeting_models.sh, "
                "or pass --asr-seconds 0")
        ok(f"SenseVoice {models[0]}")

    for path in (args.audio, args.rttm):
        if not path or not os.path.isfile(path):
            die(f"missing input {path!r}")
    with wave.open(args.audio, "rb") as wav:
        seconds = wav.getnframes() / wav.getframerate()
        ok(f"{args.audio}: {wav.getframerate()} Hz, {wav.getnchannels()} ch, "
           f"{seconds / 60:.1f} min")
    ref = read_rttm(args.rttm)
    if not ref:
        die(f"{args.rttm} has no SPEAKER lines")
    ok(f"{args.rttm}: {len(ref)} turns, {len(speech_by(ref))} speakers")


def save(args, results):
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    os.replace(tmp, args.out)


def report(name, s, c):
    info(f"{name:<34} DER {s['der']:5.1f}%   confusion {s['confusion']:5.1f}%   "
         f"found {s['speakers_found']:2d}/{s['speakers_ref']}   "
         f"consistent {c['consistent']}/{c['eligible']}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--audio")
    p.add_argument("--rttm")
    p.add_argument("--models", default=os.path.expanduser("~/diarization_models"))
    p.add_argument("--asr-model-dir", default=os.path.expanduser("~/sensevoice"))
    p.add_argument("--speakers", type=int, default=0,
                   help="people in the meeting; 0 takes the count from the RTTM, "
                        "-1 clusters by threshold")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--topic-s", type=float, default=300.0,
                   help="topic length. Real meetings with few long pauses are "
                        "cut by max_segment_s, so fixed windows are the honest case")
    p.add_argument("--minutes", type=float, default=0,
                   help="use only the first N minutes (0 = all)")
    p.add_argument("--over-factor", type=int, default=3,
                   help="design B over-clusters to this many x the count")
    p.add_argument("--min-turn-s", type=float, default=1.0,
                   help="design B embeds turns at least this long")
    p.add_argument("--max-turn-s", type=float, default=10.0,
                   help="design B embeds at most the middle this-many seconds")
    p.add_argument("--link-threshold", type=float, default=0.4,
                   help="design B's merge floor when no count is given")
    p.add_argument("--asr-seconds", type=float, default=300.0,
                   help="SenseVoice speed over this much of the start; 0 skips")
    p.add_argument("--skip-whole", action="store_true",
                   help="skip design C, the whole-meeting diarization")
    p.add_argument("--threads", type=int, default=min(4, os.cpu_count() or 2))
    p.add_argument("--out", default="results.json")
    p.add_argument("--check", action="store_true", help="preflight only")
    args = p.parse_args()

    preflight(args)
    if args.check:
        print("\nPREFLIGHT OK")
        return 0

    import numpy as np  # noqa: F401  (imported in preflight already)

    run_started = time.monotonic()
    samples = read_wav(args.audio)
    total_s = len(samples) / SAMPLE_RATE
    if args.minutes > 0:
        total_s = min(total_s, args.minutes * 60)
        samples = samples[: int(total_s * SAMPLE_RATE)]
    ref = clip(read_rttm(args.rttm), 0, total_s)
    speakers = len(speech_by(ref)) if args.speakers == 0 else args.speakers

    edges = list(np.arange(0, total_s, args.topic_s)) + [total_s]
    topics = [(float(a), float(b)) for a, b in zip(edges, edges[1:])]
    if len(topics) > 1 and topics[-1][1] - topics[-1][0] < 60:
        topics[-2] = (topics[-2][0], topics[-1][1])
        topics.pop()

    results = {
        "audio": os.path.basename(args.audio),
        "minutes": round(total_s / 60, 1),
        "speakers_given": speakers,
        "threads": args.threads,
        "topic_s": args.topic_s,
        "topics": [],
    }

    say("1. The meeting")
    info(f"{total_s / 60:.1f} min, {len(topics)} topics of {args.topic_s:.0f} s, "
         f"speakers given to clustering: {speakers}")
    for k, v in sorted(speech_by(ref).items(), key=lambda x: -x[1]):
        info(f"  {k:<8} {v / 60:5.1f} min of speech")
    for i, (a, b) in enumerate(topics):
        present = speech_by(clip(ref, a, b))
        n = sum(1 for v in present.values() if v >= 10)
        results["topics"].append({"start": a, "end": b, "speakers_present": n})
        info(f"  topic {i + 1}  {mmss(a)}-{mmss(b)}  {n} people speak >= 10 s")

    say("2. Per-topic diarization (what designs A and B both start from)")
    load_started = time.monotonic()
    diarizer = make_diarizer(args, speakers)
    ok(f"loaded in {time.monotonic() - load_started:.1f} s")
    per_topic, started = [], time.monotonic()
    for i, (a, b) in enumerate(topics):
        t0 = time.monotonic()
        segs = diarize(diarizer, samples[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)], a)
        per_topic.append(segs)
        took = time.monotonic() - t0
        info(f"  topic {i + 1}  {took:6.1f} s  (RTF {took / (b - a):.2f})  "
             f"{len({k for _, _, k in segs})} clusters, {len(segs)} turns")
    topic_s = time.monotonic() - started
    results["per_topic"] = {"seconds": round(topic_s, 1),
                            "rtf": round(topic_s / total_s, 3),
                            "peak_mb": round(peak_mb())}
    ok(f"{topic_s:.0f} s for {total_s / 60:.1f} min, RTF {topic_s / total_s:.2f}, "
       f"peak {peak_mb():.0f} MB")

    # Raw turns, so linking can be re-tried offline without re-diarizing.
    results["segments"] = {"per_topic": [
        [[round(s, 3), round(e, 3), int(k)] for s, e, k in segs]
        for segs in per_topic]}

    say("3. Design A -- per-topic numbers written straight into the record")
    hyp_a = [(s, e, k) for segs in per_topic for s, e, k in segs]
    sa, ca = score(ref, hyp_a, total_s), consistency(ref, hyp_a, topics)
    results["A"] = {"score": sa, "consistency": ca}
    report("A  per topic, as written", sa, ca)
    save(args, results)

    say("4. Design B -- per topic, then every turn linked across the meeting")
    extractor = make_extractor(args)
    started = time.monotonic()
    hyp_b, n_emb, emb_s = link_turns(extractor, samples, per_topic,
                                     speakers, args)
    link_s = time.monotonic() - started
    sb, cb = score(ref, hyp_b, total_s), consistency(ref, hyp_b, topics)
    results["B"] = {"score": sb, "consistency": cb,
                    "link_seconds": round(link_s, 1),
                    "turns_embedded": n_emb,
                    "embedded_speech_s": round(emb_s, 1)}
    ok(f"{n_emb} turns embedded ({emb_s:.0f} s of speech), "
       f"linking took {link_s:.0f} s")
    report("B  per topic + linked", sb, cb)
    save(args, results)

    if not args.skip_whole:
        say("5. Design C -- the whole meeting in one call")
        before = peak_mb()
        started = time.monotonic()
        hyp_c = diarize(diarizer, samples, 0.0, label_progress=True)
        whole_s = time.monotonic() - started
        results["segments"]["whole"] = [
            [round(s, 3), round(e, 3), int(k)] for s, e, k in hyp_c]
        sc, cc = score(ref, hyp_c, total_s), consistency(ref, hyp_c, topics)
        results["C"] = {"score": sc, "consistency": cc,
                        "seconds": round(whole_s, 1),
                        "rtf": round(whole_s / total_s, 3),
                        "peak_mb_before": round(before),
                        "peak_mb_after": round(peak_mb())}
        ok(f"{whole_s:.0f} s, RTF {whole_s / total_s:.2f}, "
           f"peak {before:.0f} -> {peak_mb():.0f} MB")
        report("C  whole meeting at once", sc, cc)
        save(args, results)

    if args.asr_seconds > 0:
        say("6. SenseVoice on the start of the meeting, labelled by design B")
        import sherpa_onnx

        model = sorted(glob.glob(os.path.join(args.asr_model_dir, "model*.onnx")))[0]
        t0 = time.monotonic()
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model,
            tokens=os.path.join(args.asr_model_dir, "tokens.txt"),
            num_threads=args.threads,
            use_itn=True,
            provider="cpu",
        )
        load_s = time.monotonic() - t0
        window = min(args.asr_seconds, total_s)
        turns = [x for x in hyp_b if x[0] < window]
        lines, spoken, started = [], 0.0, time.monotonic()
        for s, e, g in turns:
            e = min(e, window)
            if e - s < 0.3:
                continue
            stream = rec.create_stream()
            stream.accept_waveform(SAMPLE_RATE,
                                   samples[int(s * SAMPLE_RATE):int(e * SAMPLE_RATE)])
            rec.decode_stream(stream)
            spoken += e - s
            text = stream.result.text.strip()
            if text:
                who = max(clip(ref, s, e), key=lambda x: x[1] - x[0], default=None)
                lines.append((s, g, who[2] if who else "-", text))
        asr_s = time.monotonic() - started
        results["asr"] = {"model": os.path.basename(model),
                          "load_s": round(load_s, 1),
                          "window_s": round(window, 1),
                          "speech_s": round(spoken, 1),
                          "seconds": round(asr_s, 1),
                          "rtf_meeting": round(asr_s / window, 3),
                          "sample": [{"at": mmss(s), "speaker": int(g),
                                      "reference": r, "text": t}
                                     for s, g, r, t in lines[:40]]}
        ok(f"loaded in {load_s:.1f} s; {window:.0f} s of meeting "
           f"({spoken:.0f} s of turns) in {asr_s:.0f} s, "
           f"RTF {asr_s / window:.2f} of meeting time")
        for s, g, r, t in lines[:15]:
            print(f"    [{mmss(s)}] speaker {g}  (ref {r})  {t}")
        save(args, results)

    say("Summary")
    report("A  per topic, as written", sa, ca)
    report("B  per topic + linked", sb, cb)
    if "C" in results:
        report("C  whole meeting at once", results["C"]["score"],
               results["C"]["consistency"])
    info("")
    info("Who keeps one number across topics (reference -> label per topic):")
    for name, c in (("A", ca), ("B", cb)) + (
            (("C", results["C"]["consistency"]),) if "C" in results else ()):
        for k, seq in c["per_speaker"].items():
            info(f"  {name}  {k:<8} " + " ".join("." if x is None else x for x in seq))
    results["total_seconds"] = round(time.monotonic() - run_started, 1)
    save(args, results)
    ok(f"wrote {args.out}  ({results['total_seconds'] / 60:.1f} min in all)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
