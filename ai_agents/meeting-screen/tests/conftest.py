#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Shared by the screen's tests: the package and its tools on the path, the
writers the board uses loaded without their extensions (whose __init__
needs the TEN runtime), and a small finished meeting."""

import importlib
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCREEN = os.path.dirname(HERE)
EXTENSIONS = os.path.join(
    os.path.dirname(SCREEN), "agents", "ten_packages", "extension"
)
sys.path.insert(0, SCREEN)
sys.path.insert(0, os.path.join(SCREEN, "tools"))


def writer(package, module):
    """meeting_uploader.store and the like, without the package's
    __init__: a bare package whose path is the extension's folder."""
    if package not in sys.modules:
        bare = types.ModuleType(package)
        bare.__path__ = [os.path.join(EXTENSIONS, package)]
        sys.modules[package] = bare
    return importlib.import_module(f"{package}.{module}")


def finished_record():
    """Two topics, three speakers, linked and summarised."""
    return {
        "meeting_id": "m1",
        "title": "週會",
        "recorded_at": None,
        "duration_s": 120.0,
        "audio": "audio.ogg",
        "speaker_count": 3,
        "summary": "決定週三開會。",
        "topics": [
            {
                "id": "t01",
                "start_s": 0.0,
                "end_s": 60.0,
                "summary": "討論時間。",
                "error": None,
                "utterances": [
                    {
                        "speaker": 2,
                        "start_s": 1.0,
                        "end_s": 8.0,
                        "text": "我們先定時間。",
                    },
                    {
                        "speaker": 0,
                        "start_s": 9.0,
                        "end_s": 20.0,
                        "text": "週三比較好。",
                    },
                ],
            },
            {
                "id": "t02",
                "start_s": 60.0,
                "end_s": 120.0,
                "summary": "確認地點。",
                "error": None,
                "utterances": [
                    {
                        "speaker": 1,
                        "start_s": 2.0,
                        "end_s": 12.0,
                        "text": "地點在三樓。",
                    },
                    {
                        "speaker": 2,
                        "start_s": 13.0,
                        "end_s": 30.0,
                        "text": "好，就這樣。",
                    },
                ],
            },
        ],
        "actions": [],
        "actions_error": "no JSON object in the answer",
        "error": None,
    }


@pytest.fixture
def record():
    return finished_record()
