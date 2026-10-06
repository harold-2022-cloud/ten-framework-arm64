#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Action items: best effort, never at the summary's expense (PRD Part 2)."""

from meeting_control_python.actions import parse_actions


def test_actions_are_read_from_a_json_block_in_the_answer():
    answer = (
        "結論：下週出版本。\n"
        '```json\n{"待辦": [{"what": "準備發佈說明", "owner": "說話人2", '
        '"due": "下週五", "time": "12:30"}]}\n```'
    )

    actions, error = parse_actions(answer)

    assert actions == [
        {
            "what": "準備發佈說明",
            "owner": "說話人2",
            "due_raw": "下週五",
            "raised_at": "12:30",
        }
    ]
    assert error is None


def test_prose_alone_gives_no_actions_and_says_why():
    actions, error = parse_actions("結論：下週出版本。待辦由說話人2負責。")

    assert actions == []
    assert "JSON" in error


def test_a_broken_json_block_is_reported_not_raised():
    actions, error = parse_actions('結論。{"待辦": [{"what": "x"')

    assert actions == []
    assert error
