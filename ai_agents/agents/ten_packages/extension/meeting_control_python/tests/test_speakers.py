#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""One number per person across the whole meeting.

Each topic is diarized on its own, so its speaker numbers mean nothing
outside it. link() gives every turn a meeting-wide number from the turns'
voice embeddings. Vectors here are made by hand so the right answer is known.
"""

import numpy as np

from meeting_control_python.speakers import Turn, link


def unit(*v):
    v = np.asarray(v, dtype=float)
    return list(v / np.linalg.norm(v))


A = unit(1.0, 0.0, 0.0)
B = unit(0.0, 1.0, 0.0)


def turn(topic, local, start, voice, length=3.0):
    return Turn(topic, local, start, start + length, voice)


def test_one_topic_is_numbered_by_voice_too():
    turns = [turn(0, 0, 0, A), turn(0, 1, 5, B), turn(0, 0, 10, A)]

    assert link(turns, speakers=2) == [0, 1, 0]


def test_the_same_voice_in_two_topics_gets_one_number():
    # Topic 1 numbered the two people the other way round.
    turns = [
        turn(0, 0, 0, A),
        turn(0, 1, 5, B),
        turn(1, 0, 100, B),
        turn(1, 1, 105, A),
    ]

    assert link(turns, speakers=2) == [0, 1, 1, 0]


def test_numbers_follow_who_spoke_first():
    # The first to speak says least, so clustering, which ranks by speech,
    # puts them second; the record should still call them speaker 0.
    turns = [
        turn(0, 0, 0, B, length=1.5),
        turn(0, 1, 5, A, length=8.0),
        turn(1, 0, 100, A, length=8.0),
        turn(1, 1, 110, B, length=1.5),
    ]

    assert link(turns, speakers=2) == [0, 1, 1, 0]


def test_a_noisy_fragment_does_not_take_a_persons_place():
    # Two people whose voices are 0.4 alike -- the 95th percentile between
    # different people on two AISHELL-4 meetings -- and one stray turn
    # unlike either. Clustering straight to two would spend a cluster on the
    # stray and merge the two people; it must not.
    near_a = unit(1.0, 0.0, 0.0)
    near_b = unit(0.4, 0.9165, 0.0)
    stray = unit(0.05, 0.15, 1.0)  # a little closer to the second voice
    turns = []
    for i in range(10):
        turns.append(turn(i % 2, 0, i * 20.0, near_a))
        turns.append(turn(i % 2, 1, i * 20.0 + 10, near_b))
    turns.append(turn(1, 2, 400.0, stray, length=1.2))

    labels = link(turns, speakers=2)

    assert labels[:20] == [0, 1] * 10
    assert labels[20] == 1


def test_a_turn_without_an_embedding_follows_its_topic_cluster():
    turns = [
        turn(0, 0, 0, A),
        turn(0, 1, 5, B),
        turn(1, 0, 100, B),
        turn(1, 0, 104, None, length=0.6),  # too short to embed
        turn(1, 1, 110, A),
    ]

    assert link(turns, speakers=2)[3] == 1


def test_a_cluster_with_no_embedding_takes_the_nearest_turn_in_time():
    turns = [
        turn(0, 0, 0, A),
        turn(1, 0, 100, B),
        turn(1, 1, 109, None, length=0.5),  # its whole cluster is short
        turn(1, 0, 110, B),
        turn(1, 2, 200, A),
    ]

    assert link(turns, speakers=2)[2] == 1


def test_without_a_count_the_threshold_decides():
    turns = [turn(0, 0, 0, A), turn(1, 0, 100, B), turn(1, 1, 105, A)]

    assert link(turns, speakers=-1) == [0, 1, 0]


def test_a_short_meeting_does_not_keep_two_pieces_of_one_voice():
    # Too few turns for over-clustering to merge anything: the two biggest
    # clusters are both the first voice. Turns that are clearly one voice
    # are merged regardless.
    turns = [
        turn(0, 0, 0, A, length=8.0),
        turn(0, 1, 10, B, length=2.0),
        turn(1, 0, 100, A, length=8.0),
        turn(1, 1, 110, B, length=2.0),
    ]

    assert link(turns, speakers=2) == [0, 1, 0, 1]
