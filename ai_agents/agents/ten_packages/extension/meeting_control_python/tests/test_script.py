#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The record in the script the user chose: Traditional or Simplified.

The models write Simplified -- SenseVoice's transcript and the board LLM's
summaries alike -- and the prompts stay Simplified, because that is what
was measured on the board to work. The choice is applied once, to the
finished record, so every part of it reads the same way.
"""

import pytest

from meeting_control_python.script import convert_record, converter


def test_traditional_gives_the_models_text_in_taiwan_characters():
    assert converter("traditional")("说话人3建议调整孩子年龄范围") == (
        "說話人3建議調整孩子年齡範圍"
    )


def test_simplified_turns_the_records_own_labels_simplified_too():
    assert converter("simplified")("說話人1：結論") == "说话人1：结论"


def test_an_unknown_script_is_refused_by_name():
    with pytest.raises(ValueError, match="cantonese"):
        converter("cantonese")


def test_the_record_is_converted_and_its_identifiers_are_not():
    record = {
        "meeting_id": "会议-1",
        "title": "会议",
        "audio": "audio.ogg",
        "summary": "会议结论",
        "topics": [
            {
                "id": "t01",
                "summary": "重点",
                "error": None,
                "utterances": [{"text": "说话", "speaker": 0, "start_s": 1.0}],
            }
        ],
        "actions": [{"what": "调整"}],
        "actions_error": "no JSON object in the answer",
        "error": None,
    }

    out = convert_record(record, converter("traditional"))

    assert out["summary"] == "會議結論"
    assert out["title"] == "會議"
    assert out["topics"][0]["summary"] == "重點"
    assert out["topics"][0]["utterances"][0]["text"] == "說話"
    assert out["topics"][0]["utterances"][0]["speaker"] == 0
    assert out["actions"][0]["what"] == "調整"
    assert out["meeting_id"] == "会议-1"
    assert out["topics"][0]["id"] == "t01"
    assert out["audio"] == "audio.ogg"
    assert out["actions_error"] == "no JSON object in the answer"
