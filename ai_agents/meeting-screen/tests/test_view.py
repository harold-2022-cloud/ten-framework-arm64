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


def test_a_finished_meeting_stays_for_recent_min_two_hours_by_default():
    assert build([m("a", "archived", NOW - 119 * 60)])["scene"] == "result"
    assert build([m("a", "archived", NOW - 121 * 60)])["scene"] == "ready"
    assert (
        build([m("a", "archived", NOW - 61 * 60)], recent_min=60)["scene"]
        == "ready"
    )


def test_an_unfinished_meeting_wins_over_a_newer_finished_one():
    v = build([m("a", "summarising", NOW - 100), m("b", "archived", NOW - 10)])
    assert v["meeting"]["meeting_id"] == "a"


def test_the_showcase_takes_no_part_in_auto_mode():
    v = build(
        [m("live", "archived", NOW - 121 * 60), m("show", "archived", NOW - 5)],
        showcase="show",
    )
    assert v["scene"] == "ready"
    assert v["newest_received_at"] == NOW - 121 * 60
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
        7,
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
        ("linking", 4),
        ("summarising", 5),
        ("concluding", 6),
        ("archived", 7),
        ("something-new", 1),
    ):
        assert (
            build([m("a", state, NOW - 5)])["meeting"]["stage"] == stage
        ), state
    assert build([m("a", "something-new", NOW - 5)])["scene"] == "processing"


def test_processing_time():
    done = build(
        [
            m(
                "a",
                "archived",
                NOW - 500,
                started_at=NOW - 400,
                updated_at=NOW - 100,
            )
        ]
    )["meeting"]
    assert (done["finished_at"], done["elapsed_s"]) == (NOW - 100, 300.0)
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
        "speakers": None,
        "showcase": True,
    }


def test_listing_says_how_many_speakers_a_finished_meeting_found():
    rows = view.listing(
        [m("a", "archived", 1), m("b", "transcribing", 2)],
        read_record=lambda i: {"speaker_count": 9} if i == "a" else None,
    )
    assert [(r["meeting_id"], r["speakers"]) for r in rows] == [
        ("b", None),
        ("a", 9),
    ]


def test_archived_with_no_words_is_a_failure_with_the_topic_error():
    # Every topic's transcription failed: the controller still ends it
    # archived (flow.py mark_failed, then link([]) and the conclusion).
    failed = {"topics": [{"error": "ASR timed out", "utterances": []}]}
    v = build([m("a", "archived", NOW - 5)], records={"a": failed})
    assert (v["scene"], v["meeting"]["error"]) == ("failed", "ASR timed out")
    v = build([m("a", "archived", NOW - 5)], records={"a": {"topics": []}})
    assert (v["scene"], v["meeting"]["error"]) == ("failed", "沒有轉出任何文字")


def test_archived_with_words_or_no_record_yet_is_the_result(record):
    assert (
        build([m("a", "archived", NOW - 5)], records={"a": record})["scene"]
        == "result"
    )
    assert build([m("a", "archived", NOW - 5)])["scene"] == "result"


def test_recognition_is_its_own_stage_with_its_turns():
    v = build(
        [
            m(
                "a",
                "transcribing",
                NOW - 5,
                step="recognising",
                step_started_at=NOW - 20,
                topic_s=300,
                turns_done=3,
                turns_total=10,
            )
        ]
    )["meeting"]
    assert (v["stage"], v["step"], v["turns"]) == (
        3,
        "recognising",
        {"done": 3, "total": 10},
    )
    assert (v["step_started_at"], v["topic_s"]) == (NOW - 20, 300.0)
    diarizing = build([m("a", "transcribing", NOW - 5, step="diarizing")])
    assert diarizing["meeting"]["stage"] == 2


def test_a_step_left_over_from_transcribing_is_not_shown_later():
    v = build([m("a", "summarising", NOW - 5, step="recognising")])["meeting"]
    assert (v["stage"], v["step"], v["step_started_at"]) == (5, None, None)


def test_when_the_current_state_began_is_passed_on():
    v = build([m("a", "concluding", NOW - 5, updated_at=NOW - 30)])
    assert v["meeting"]["since"] == NOW - 30


def test_a_long_meeting_stays_for_recent_min_from_when_it_ended():
    # Received 87 minutes ago, done a minute ago: an 86-minute meeting.
    done_now = m("a", "archived", NOW - 87 * 60, updated_at=NOW - 60)
    assert build([done_now])["scene"] == "result"
    done_long_ago = m(
        "a", "archived", NOW - 250 * 60, updated_at=NOW - 121 * 60
    )
    assert build([done_long_ago])["scene"] == "ready"
    failed_now = m("a", "failed", NOW - 200 * 60, updated_at=NOW - 5, error="x")
    assert build([failed_now])["scene"] == "failed"


# The board last booted 5 minutes before NOW.
BOOT = NOW - 300.0


def test_a_meeting_left_mid_way_before_the_boot_shows_as_cut_off():
    # Power went at NOW - 400: nothing that ran then runs now. The next
    # worker marks it failed when it starts (store.py fail_interrupted);
    # until then it must not read "processing".
    left = m(
        "a",
        "transcribing",
        NOW - 900,
        started_at=NOW - 890,
        step="diarizing",
        step_started_at=NOW - 500,
        updated_at=NOW - 400,
    )
    v = build([left], booted_at=BOOT)
    assert (v["scene"], v["meeting"]["state"], v["meeting"]["stage"]) == (
        "failed",
        "failed",
        7,
    )
    assert v["meeting"]["error"] == view.INTERRUPTED_ZH


def test_a_meeting_worked_on_since_the_boot_is_still_processed():
    going = m("a", "transcribing", NOW - 900, updated_at=NOW - 100)
    assert build([going], booted_at=BOOT)["scene"] == "processing"
    before = m("a", "transcribing", NOW - 900, updated_at=NOW - 400)
    assert build([before])["scene"] == "processing"


def test_a_meeting_queued_before_the_boot_still_waits_its_turn():
    queued = m("q", "queued", NOW - 900, updated_at=NOW - 900)
    assert build([queued], booted_at=BOOT)["scene"] == "queued"


def test_an_interrupted_failure_counts_from_when_it_was_last_worked_on():
    # prepare --warm starts a worker the next morning, and the worker
    # stamps yesterday's interrupted meeting failed now (updated_at):
    # no fresh failure to open the demo with.
    day = 24 * 3600.0
    yesterday = m(
        "a",
        "failed",
        NOW - day,
        started_at=NOW - day + 10,
        step_started_at=NOW - day + 600,
        updated_at=NOW - 60,
        error=view.INTERRUPTED,
    )
    assert build([yesterday])["scene"] == "ready"
    just_now = m(
        "a",
        "failed",
        NOW - 3000,
        started_at=NOW - 2990,
        step_started_at=NOW - 600,
        updated_at=NOW - 60,
        error=view.INTERRUPTED,
    )
    v = build([just_now])["meeting"]
    assert (v["error"], v["finished_at"]) == (view.INTERRUPTED_ZH, NOW - 600)


def test_the_list_says_a_meeting_cut_off_by_the_boot_did_not_finish():
    rows = view.listing(
        [m("a", "summarising", NOW - 900, updated_at=NOW - 400)],
        booted_at=BOOT,
    )
    assert rows[0]["state"] == "failed"
