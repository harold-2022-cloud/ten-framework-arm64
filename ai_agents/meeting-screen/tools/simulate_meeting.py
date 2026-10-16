#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Replay a finished meeting into a meetings folder, in the order the board
writes it, so the screen can be watched without a board or a phone.

  python3 tools/simulate_meeting.py RECORD.json --meetings-dir /tmp/sim
  python3 -m meeting_screen --meetings-dir /tmp/sim   # http://localhost:8766/

queued, received, decoding, transcribing topic by topic (each topic numbering
its own speakers, as before linking), linking, summarising topic by topic,
concluding, archived: about --seconds (120) in all. Each file is written to
a temporary name and renamed, as the uploader and the controller write it,
the record before the state. Standard library only.
"""

import argparse
import copy
import json
import os
import sys
import tempfile
import time
from typing import Iterator, Optional, Tuple

Step = Tuple[str, dict, Optional[dict]]


def _own_numbers(utterances):
    """A topic's own speaker numbers, from 0 in order of first word."""
    seen = {}
    return [
        {**u, "speaker": seen.setdefault(u.get("speaker"), len(seen))}
        for u in utterances
    ]


def _partial(record, topics, summaries=0, conclusion=""):
    out = copy.deepcopy(record)
    out["topics"] = copy.deepcopy(topics)
    for i, topic in enumerate(out["topics"]):
        if i >= summaries:
            topic["summary"] = ""
    out["summary"] = conclusion
    out["speaker_count"] = len(
        {
            u.get("speaker")
            for t in out["topics"]
            for u in t.get("utterances", [])
        }
    )
    return out


def steps(record: dict) -> Iterator[Step]:
    """(state, fields merged into state.json, record.json or None)."""
    topics = record.get("topics") or []
    total = len(topics)
    yield "received", {}, None
    yield "decoding", {}, None
    yield "transcribing", {"topics_done": 0, "topics_total": total}, None
    own = []
    for i, topic in enumerate(topics):
        own.append(
            {**topic, "utterances": _own_numbers(topic.get("utterances") or [])}
        )
        yield "transcribing", {"topics_done": i + 1}, _partial(record, own)
    yield "linking", {}, _partial(record, own)
    yield "summarising", {"topics_done": 0, "topics_total": total}, _partial(
        record, topics
    )
    for i in range(total):
        yield "summarising", {"topics_done": i + 1}, _partial(
            record, topics, i + 1
        )
    yield "concluding", {}, _partial(record, topics, total)
    yield "archived", {}, copy.deepcopy(record)


def _write_json(path, value):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
    os.replace(tmp, path)


def run(
    record: dict,
    meetings_dir: str,
    meeting_id: str,
    seconds: float = 120.0,
    clock=time.time,
    sleep=time.sleep,
    say=print,
) -> str:
    folder = os.path.join(meetings_dir, meeting_id)
    os.makedirs(os.path.join(folder, "work"), exist_ok=True)
    state_path = os.path.join(folder, "work", "state.json")
    record_path = os.path.join(folder, "record.json")
    title = record.get("title") or "模擬會議"
    received = clock()
    _write_json(
        os.path.join(folder, "upload.json"),
        {
            "title": title,
            "speakers": record.get("speaker_count"),
            "duration_s": float(record.get("duration_s") or 0.0),
        },
    )
    state = {
        "meeting_id": meeting_id,
        "state": "queued",
        "title": title,
        "received_at": received,
        "topics_done": 0,
        "topics_total": 0,
        "error": None,
        "updated_at": received,
    }
    _write_json(state_path, state)
    say(f"queued        {folder}")
    plan = list(steps(record))
    pause = seconds / max(1, len(plan))
    for name, fields, partial in plan:
        sleep(pause)
        now = clock()
        if name == "received":
            state["started_at"] = now
        state.update(fields)
        state["state"] = name
        state["updated_at"] = now
        if partial is not None:
            _write_json(record_path, partial)
        _write_json(state_path, state)
        say(f"{name:<13} {state['topics_done']}/{state['topics_total']}")
    return folder


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("record", help="a finished meeting's record.json")
    p.add_argument("--meetings-dir", required=True)
    p.add_argument(
        "--id", default=None, help="the meeting's id (default: sim-<time>)"
    )
    p.add_argument(
        "--seconds", type=float, default=120.0, help="the whole replay (120)"
    )
    args = p.parse_args(argv)
    with open(args.record, encoding="utf-8") as f:
        record = json.load(f)
    meeting_id = args.id or time.strftime("sim-%Y%m%d-%H%M%S")
    run(record, args.meetings_dir, meeting_id, args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
