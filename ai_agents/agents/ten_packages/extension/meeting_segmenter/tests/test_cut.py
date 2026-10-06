#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""How a meeting's speech becomes topics.

Topics tile the file: every second of the recording belongs to exactly one
topic, and boundaries go in the middle of silences. Nothing is dropped on the
VAD's word -- on far-field AISHELL-4 audio a VAD at threshold 0.5 heard only
37-50 % of the speech, and topics trimmed to what it heard left 20 % of the
meeting out of the transcript.

cut() sees only where speech is, in seconds of the file. Every expected
value below is worked out by hand from the runs it is given.
"""

from meeting_segmenter.segmenter import cut

SILENCE_S = 30.0
MIN_S = 5.0
MAX_S = 600.0


def topics(runs, total_s):
    return cut(runs, total_s, silence_s=SILENCE_S, min_s=MIN_S, max_s=MAX_S)


def test_no_speech_means_no_topics():
    assert topics([], total_s=120.0) == []


def test_pauses_shorter_than_the_silence_stay_in_one_topic():
    # 10 s of quiet between the runs: someone reading, not a new subject.
    assert topics([(1.0, 10.0), (20.0, 40.0)], total_s=45.0) == [(0.0, 45.0)]


def test_quiet_before_the_first_word_and_after_the_last_is_kept():
    assert topics([(10.0, 50.0)], total_s=100.0) == [(0.0, 100.0)]


def test_a_long_enough_silence_ends_a_topic_at_its_middle():
    # Quiet from 100 s to 140 s: the boundary goes at 120 s.
    assert topics([(0.0, 100.0), (140.0, 300.0)], total_s=300.0) == [
        (0.0, 120.0),
        (120.0, 300.0),
    ]


def test_a_silence_of_exactly_the_threshold_splits():
    assert topics([(0.0, 100.0), (130.0, 300.0)], total_s=300.0) == [
        (0.0, 115.0),
        (115.0, 300.0),
    ]


def test_a_topic_with_too_little_speech_joins_the_next():
    # A 3 s cough, then quiet, then the meeting. The first topic would span
    # 0-21.5 s but holds only 3 s of speech: too little to summarise.
    assert topics([(0.0, 3.0), (40.0, 200.0)], total_s=200.0) == [(0.0, 200.0)]


def test_a_last_topic_with_too_little_speech_joins_the_previous():
    assert topics([(0.0, 200.0), (240.0, 243.0)], total_s=250.0) == [
        (0.0, 250.0)
    ]


def test_a_short_topic_stays_alone_when_joining_would_exceed_max():
    # Joining would make 0-615, longer than the 600 s ceiling.
    assert topics([(0.0, 3.0), (40.0, 615.0)], total_s=615.0) == [
        (0.0, 21.5),
        (21.5, 615.0),
    ]


def test_an_overlong_topic_is_cut_in_its_longest_pause():
    # Pauses of 5 s and 12 s, both too short to end a topic, inside a 700 s
    # stretch. The cut goes in the middle of the 12 s one, at 506 s.
    assert topics(
        [(0.0, 300.0), (305.0, 500.0), (512.0, 700.0)], total_s=700.0
    ) == [(0.0, 506.0), (506.0, 700.0)]


def test_an_overlong_topic_with_no_pause_is_cut_at_the_max():
    assert topics([(0.0, 1300.0)], total_s=1300.0) == [
        (0.0, 600.0),
        (600.0, 1200.0),
        (1200.0, 1300.0),
    ]


def test_a_pause_past_the_max_is_not_used_for_the_cut():
    # The only pause is at 650-660 s. Cutting there would leave a 655 s
    # topic, over the ceiling, so the cut is at 600 s instead.
    assert topics([(0.0, 650.0), (660.0, 700.0)], total_s=700.0) == [
        (0.0, 600.0),
        (600.0, 700.0),
    ]


def test_a_pause_in_the_first_min_seconds_is_not_used_for_the_cut():
    # The pause at 1-3 s would cut a 2 s sliver off the front. The other,
    # at 610-613 s, is past the ceiling.
    assert topics(
        [(0.0, 1.0), (3.0, 610.0), (613.0, 700.0)], total_s=700.0
    ) == [(0.0, 600.0), (600.0, 700.0)]


def test_a_length_cut_prefers_a_pause_in_the_second_half():
    # The 8 s pause at 20-28 s is the longest, but cutting there would leave
    # a 24 s topic. The 2 s pause at 400-402 s keeps the piece near the
    # ceiling, which is what a cut made only for memory should do.
    assert topics(
        [(0.0, 20.0), (28.0, 400.0), (402.0, 700.0)], total_s=700.0
    ) == [(0.0, 401.0), (401.0, 700.0)]


def test_a_length_cut_falls_back_to_any_pause_before_cutting_a_sentence():
    # No pause in the second half; the one at 100-110 s still beats cutting
    # someone off at 600 s.
    assert topics([(0.0, 100.0), (110.0, 650.0)], total_s=650.0) == [
        (0.0, 105.0),
        (105.0, 650.0),
    ]
