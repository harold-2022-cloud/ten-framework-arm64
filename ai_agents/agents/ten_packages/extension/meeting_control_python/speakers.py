#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""One number per person across the whole meeting.

Each topic is diarized on its own, so its speaker numbers mean nothing
outside it: "speaker 1" in one topic and in the next need not be the same
person. Written into the record as they are, they put 54-57 % of the speech
on the wrong person (AISHELL-4, PRD Part 2).

So every turn of the meeting is clustered together by its voice embedding.
Clustering straight to the head count lets a few noisy turns hold clusters
of their own to the end, and real people get merged instead -- 28 % wrong on
the same meeting. Over-clustering to three times the count, keeping the
clusters that hold the most speech and folding the rest into the nearest
kept one brought that to 1.2 %, and 0.8 % on a 7-person meeting run with
the parameters fixed beforehand.
"""

import bisect
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np


@dataclass
class Turn:
    topic: int
    local: int  # the speaker number diarization gave it inside its topic
    start_s: float  # in the meeting, not the topic
    end_s: float
    embedding: Optional[Sequence[float]]


def link(
    turns: List[Turn],
    speakers: int,
    over_factor: int = 3,
    threshold: float = 0.4,
    same_voice: float = 0.55,
    max_turn_s: float = 10.0,
) -> List[int]:
    """A meeting-wide speaker number for every turn, in the order given.

    Numbers follow who spoke first. With speakers <= 0 the clusters are cut
    at threshold instead of at a count.
    """
    if not turns:
        return []

    owner = [i for i, t in enumerate(turns) if t.embedding is not None]
    if not owner:
        return _by_first_appearance(turns, [t.local for t in turns])
    x = np.asarray([turns[i].embedding for i in owner], dtype=np.float64)
    w = np.asarray(
        [min(turns[i].end_s - turns[i].start_s, max_turn_s) for i in owner]
    )

    if speakers > 0:
        groups = _average_linkage(x, w, speakers * over_factor, same_voice)
        groups.sort(key=lambda m: -w[m].sum())
        keep, rest = groups[:speakers], groups[speakers:]
    else:
        keep, rest = _average_linkage(x, w, -1, threshold), []

    cents = np.asarray([_centroid(x, w, m) for m in keep])
    label: Dict[int, int] = {}
    for g, members in enumerate(keep):
        for m in members:
            label[owner[m]] = g
    for members in rest:
        g = int(np.argmax(cents @ _centroid(x, w, members)))
        for m in members:
            label[owner[m]] = g

    return _by_first_appearance(turns, _fill_unembedded(turns, label))


def _centroid(x: np.ndarray, w: np.ndarray, members: List[int]) -> np.ndarray:
    c = (x[members] * w[members, None]).sum(0)
    return c / np.linalg.norm(c)


def _average_linkage(
    x: np.ndarray, w: np.ndarray, n_clusters: int, threshold: float
) -> List[List[int]]:
    """Weighted average linkage on cosine similarity. Merges while more than
    n_clusters remain, and past that for as long as the best pair is at
    least threshold alike; with n_clusters <= 0, only by threshold.

    The second rule is what a short meeting needs: with fewer turns than
    n_clusters nothing would merge, and the clusters kept as people would be
    pieces of one voice. 0.55 is above the 99th percentile of similarity
    between different people on two AISHELL-4 meetings (0.51-0.54), where
    half of a person's own turns are 0.58-0.65 alike or more.

    A running matrix of summed pair similarities makes each merge one
    vectorised pass rather than a loop over every pair of clusters."""
    pair_sum = (x @ x.T) * w[:, None] * w[None, :]
    np.fill_diagonal(pair_sum, -np.inf)
    size = w.astype(np.float64).copy()
    alive = np.ones(len(x), dtype=bool)
    members = [[i] for i in range(len(x))]
    while alive.sum() > 1:
        avg = pair_sum / (size[:, None] * size[None, :])
        avg[~alive, :] = -np.inf
        avg[:, ~alive] = -np.inf
        i, j = divmod(int(np.argmax(avg)), avg.shape[1])
        above_target = n_clusters > 0 and alive.sum() > n_clusters
        if not above_target and avg[i, j] < threshold:
            break
        pair_sum[i, :] += pair_sum[j, :]
        pair_sum[:, i] += pair_sum[:, j]
        pair_sum[i, i] = -np.inf
        size[i] += size[j]
        alive[j] = False
        members[i] += members[j]
        members[j] = []
    return [m for m in members if m]


def _fill_unembedded(turns: List[Turn], label: Dict[int, int]) -> List[int]:
    """Turns too short to embed take the number most of their own (topic,
    speaker) got; failing that, the nearest labelled turn in time."""
    votes: Dict[tuple, Dict[int, float]] = {}
    for i, g in label.items():
        key = (turns[i].topic, turns[i].local)
        bucket = votes.setdefault(key, {})
        bucket[g] = bucket.get(g, 0.0) + turns[i].end_s - turns[i].start_s
    timeline = sorted((turns[i].start_s, g) for i, g in label.items())
    starts = [s for s, _ in timeline]
    out: List[int] = []
    for i, t in enumerate(turns):
        if i in label:
            out.append(label[i])
            continue
        bucket = votes.get((t.topic, t.local))
        if bucket:
            out.append(max(bucket, key=bucket.get))
            continue
        j = bisect.bisect_left(starts, t.start_s)
        near = [timeline[c] for c in (j - 1, j) if 0 <= c < len(timeline)]
        out.append(min(near, key=lambda c, at=t.start_s: abs(c[0] - at))[1])
    return out


def _by_first_appearance(turns: List[Turn], labels: List[int]) -> List[int]:
    """Renumber so speaker 0 is whoever spoke first, 1 the next, and so on."""
    order: Dict[int, int] = {}
    for i in sorted(range(len(turns)), key=lambda k: turns[k].start_s):
        order.setdefault(labels[i], len(order))
    return [order[g] for g in labels]
