#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""simulate_meeting.py: a finished meeting replayed in the order the board
writes it -- each topic numbering its own speakers until linking, a summary
at a time, the conclusion last -- and every step a view the screen draws."""

import itertools

import simulate_meeting as sim

from meeting_screen import disk, view


def names(record):
    return [k for k, _ in itertools.groupby(s for s, _, _ in sim.steps(record))]


def test_the_states_come_in_the_boards_order(record):
    assert names(record) == [
        "received",
        "decoding",
        "transcribing",
        "linking",
        "summarising",
        "concluding",
        "archived",
    ]


def test_speakers_are_each_topics_own_until_linked(record):
    for state, _, rec in sim.steps(record):
        if rec is None:
            continue
        firsts = [
            t["utterances"][0]["speaker"]
            for t in rec["topics"]
            if t["utterances"]
        ]
        if state in ("transcribing", "linking"):
            assert all(f == 0 for f in firsts), (state, firsts)
        else:
            assert firsts == [2, 1], (state, firsts)


def test_summaries_one_topic_at_a_time_and_the_conclusion_last(record):
    seen = []
    for state, _, rec in sim.steps(record):
        if rec is not None:
            seen.append(
                (
                    state,
                    sum(1 for t in rec["topics"] if t["summary"]),
                    bool(rec["summary"]),
                )
            )
    assert ("summarising", 0, False) in seen
    assert ("summarising", 1, False) in seen
    assert ("concluding", 2, False) in seen
    assert seen[-1] == ("archived", 2, True)


def test_a_run_ends_as_the_record_and_the_screen_shows_it(tmp_path, record):
    sim.run(
        record,
        str(tmp_path),
        "sim-1",
        seconds=0,
        sleep=lambda s: None,
        say=lambda line: None,
    )
    assert disk.read_record(str(tmp_path), "sim-1") == record
    meetings = disk.list_meetings(str(tmp_path))
    v = view.build(
        meetings,
        lambda i: disk.read_record(str(tmp_path), i),
        meetings[0].received_at + 5,
    )
    assert (v["scene"], v["meeting"]["speakers_final"]) == ("result", True)


def test_every_step_is_a_view_the_screen_can_draw(tmp_path, record):
    scenes = []

    def look(_):
        meetings = disk.list_meetings(str(tmp_path))
        v = view.build(
            meetings,
            lambda i: disk.read_record(str(tmp_path), i),
            meetings[0].received_at + 5,
        )
        scenes.append((v["scene"], v["meeting"]["stage"]))

    sim.run(
        record,
        str(tmp_path),
        "sim-2",
        seconds=0,
        sleep=look,
        say=lambda line: None,
    )
    look(None)
    assert scenes[0] == ("queued", 0)
    assert [s for s, _ in itertools.groupby(stage for _, stage in scenes)] == [
        0,
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    assert scenes[-1] == ("result", 6)
