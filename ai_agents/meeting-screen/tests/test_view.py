#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""view.py: which meeting, which scene, which stage; whether its speakers
can be shown; the processing time; a queued meeting's place."""

from meeting_screen import view
from meeting_screen.disk import Meeting

NOW = 10_000.0


def m(meeting_id, state, received_at, upload=None, **fields):
    return Meeting(
        meeting_id,
        {"state": state, "received_at": received_at, **fields},
        upload or {},
    )


def build(meetings, records=None, **kw):
    newest_first = sorted(meetings, key=lambda x: x.received_at, reverse=True)
    return view.build(newest_first, lambda i: (records or {}).get(i), NOW, **kw)


def test_nothing_is_the_ready_screen():
    v = build([])
    assert (v["scene"], v["meeting"], v["newest_received_at"]) == (
        "ready",
        None,
        None,
    )


def test_the_newest_unfinished_meeting_comes_first():
    v = build([m("a", "transcribing", NOW - 100), m("b", "queued", NOW - 10)])
    assert v["scene"] == "queued"
    assert v["meeting"]["meeting_id"] == "b"
    assert v["meeting"]["ahead"] == 1


def test_a_finished_meeting_stays_for_recent_min():
    assert build([m("a", "archived", NOW - 30 * 60)])["scene"] == "result"
    assert build([m("a", "archived", NOW - 61 * 60)])["scene"] == "ready"
    assert (
        build([m("a", "archived", NOW - 61 * 60)], recent_min=90)["scene"]
        == "result"
    )


def test_an_unfinished_meeting_wins_over_a_newer_finished_one():
    v = build([m("a", "summarising", NOW - 100), m("b", "archived", NOW - 10)])
    assert v["meeting"]["meeting_id"] == "a"


def test_the_showcase_takes_no_part_in_auto_mode():
    v = build(
        [m("live", "archived", NOW - 61 * 60), m("show", "archived", NOW - 5)],
        showcase="show",
    )
    assert v["scene"] == "ready"
    assert v["newest_received_at"] == NOW - 61 * 60
    assert v["showcase_id"] == "show"


def test_a_showcase_not_on_disk_is_no_showcase():
    assert (
        build([m("a", "archived", NOW - 5)], showcase="gone")["showcase_id"]
        is None
    )


def test_a_pin_shows_its_meeting_the_showcase_included():
    v = build(
        [m("live", "transcribing", NOW - 5), m("show", "archived", NOW - 9999)],
        showcase="show",
        pin="show",
    )
    assert (v["pinned"], v["scene"], v["meeting"]["meeting_id"]) == (
        True,
        "result",
        "show",
    )
    assert v["meeting"]["showcase"] is True


def test_failed_and_empty_are_the_failed_scene_with_the_reason():
    v = build([m("a", "failed", NOW - 5, error="ASR failed")])
    assert (v["scene"], v["meeting"]["error"], v["meeting"]["stage"]) == (
        "failed",
        "ASR failed",
        6,
    )
    assert build([m("a", "empty", NOW - 5)])["scene"] == "failed"


def test_speaker_numbers_are_final_only_once_linked():
    for state, final in (
        ("queued", False),
        ("received", False),
        ("decoding", False),
        ("transcribing", False),
        ("linking", False),
        ("summarising", True),
        ("concluding", True),
        ("archived", True),
        ("failed", False),
        ("empty", False),
    ):
        assert (
            build([m("a", state, NOW - 5)])["meeting"]["speakers_final"]
            is final
        ), state


def test_each_state_lights_its_stage():
    for state, stage in (
        ("queued", 0),
        ("received", 1),
        ("decoding", 1),
        ("transcribing", 2),
        ("linking", 3),
        ("summarising", 4),
        ("concluding", 5),
        ("archived", 6),
        ("something-new", 1),
    ):
        assert (
            build([m("a", state, NOW - 5)])["meeting"]["stage"] == stage
        ), state
    assert build([m("a", "something-new", NOW - 5)])["scene"] == "processing"


def test_processing_time():
    done = build(
        [m("a", "archived", NOW - 5, started_at=100.0, updated_at=400.0)]
    )["meeting"]
    assert (done["finished_at"], done["elapsed_s"]) == (400.0, 300.0)
    going = build(
        [
            m(
                "a",
                "transcribing",
                NOW - 5,
                started_at=NOW - 150,
                updated_at=NOW - 1,
            )
        ]
    )["meeting"]
    assert (going["finished_at"], going["elapsed_s"]) == (None, 150.0)
    waiting = build([m("a", "queued", NOW - 5)])["meeting"]
    assert waiting["elapsed_s"] is None


def test_a_queued_meetings_place():
    meetings = [
        m("busy", "summarising", NOW - 300),
        m("q1", "queued", NOW - 200),
        m("q2", "queued", NOW - 100),
        m("done", "archived", NOW - 400),
    ]
    v = build(meetings, pin="q2")
    assert v["meeting"]["ahead"] == 2
    assert build(meetings, pin="q1")["meeting"]["ahead"] == 1


def test_title_and_duration_fall_back_to_the_upload_then_the_record():
    record = {"title": "記錄的標題", "duration_s": 77.0}
    v = build([m("a", "archived", NOW - 5)], records={"a": record})["meeting"]
    assert (v["title"], v["duration_s"], v["record"]) == (
        "記錄的標題",
        77.0,
        record,
    )
    v = build(
        [
            m(
                "a",
                "archived",
                NOW - 5,
                upload={"title": "上傳的", "duration_s": 60},
            )
        ]
    )["meeting"]
    assert (v["title"], v["duration_s"]) == ("上傳的", 60.0)
    v = build(
        [
            m(
                "a",
                "archived",
                NOW - 5,
                title="狀態的",
                upload={"title": "上傳的"},
            )
        ]
    )["meeting"]
    assert v["title"] == "狀態的"


def test_progress_counts_are_numbers():
    v = build([m("a", "transcribing", NOW - 5, topics_done=2, topics_total=5)])[
        "meeting"
    ]
    assert v["progress"] == {"topics_done": 2, "topics_total": 5}
    v = build([m("a", "transcribing", NOW - 5, topics_done="x")])["meeting"]
    assert v["progress"] == {"topics_done": 0, "topics_total": 0}


def test_listing_newest_first_with_the_showcase_marked():
    rows = view.listing(
        [m("a", "archived", 1, upload={"duration_s": 9}), m("b", "queued", 2)],
        showcase="a",
    )
    assert [r["meeting_id"] for r in rows] == ["b", "a"]
    assert rows[1] == {
        "meeting_id": "a",
        "title": "",
        "state": "archived",
        "received_at": 1.0,
        "duration_s": 9.0,
        "showcase": True,
    }
