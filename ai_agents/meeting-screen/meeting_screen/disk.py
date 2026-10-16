#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The meetings on the board's disk, read only.

    <meetings_dir>/<meeting_id>/
        upload.json      the uploader's, when the meeting landed
        work/state.json  how far it got (the uploader, then the controller)
        record.json      the controller's, as it grows and when it ends

Every file is written whole -- a temporary file renamed into place -- yet a
read can still meet one missing, empty or of another shape: that is "not
yet", never an error. Standard library only: the board runs this under its
own python3, which has none of the packages the meeting extensions use.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

# The uploader's rule (meeting_uploader/store.py): starts with a letter or a
# digit, so "." and ".." are not ids; no slashes.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def valid_id(meeting_id) -> bool:
    return isinstance(meeting_id, str) and bool(_ID.fullmatch(meeting_id))


@dataclass
class Meeting:
    meeting_id: str
    state: dict
    upload: dict = field(default_factory=dict)

    @property
    def received_at(self) -> float:
        value = self.state.get("received_at")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)


def _json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_meeting(meetings_dir: str, meeting_id) -> Optional[Meeting]:
    if not valid_id(meeting_id):
        return None
    folder = os.path.join(meetings_dir, meeting_id)
    state = _json(os.path.join(folder, "work", "state.json"))
    if state is None:
        return None
    upload = _json(os.path.join(folder, "upload.json")) or {}
    return Meeting(meeting_id, state, upload)


def list_meetings(meetings_dir: str) -> List[Meeting]:
    """Every meeting with a state, the newest received first."""
    try:
        names = sorted(os.listdir(meetings_dir))
    except OSError:
        return []
    found = [read_meeting(meetings_dir, name) for name in names]
    meetings = [m for m in found if m is not None]
    meetings.sort(key=lambda m: (m.received_at, m.meeting_id), reverse=True)
    return meetings


def read_record(meetings_dir: str, meeting_id) -> Optional[dict]:
    if not valid_id(meeting_id):
        return None
    return _json(os.path.join(meetings_dir, meeting_id, "record.json"))
