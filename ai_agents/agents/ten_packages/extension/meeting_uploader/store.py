#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The meetings on disk: what is let in, where it lands, how far it got.

    <meetings_dir>/<meeting_id>/
        audio.ogg       the upload, byte for byte
        upload.json     what came with it, kept for its turn in the queue
        record.json     written by the controller when it ends
        minutes.txt     likewise
        work/state.json how far processing got; GET /meeting/{id} reads it

Nothing here knows TEN or HTTP, so all of it is tested on plain files.
"""

import json
import os
import re
import shutil
import tempfile
import time
from typing import List, Optional, Tuple

import soundfile as sf

# States the controller leaves a meeting in for good. Anything else but
# QUEUED means a worker is still on it -- or was, until it was stopped.
TERMINAL = ("archived", "empty", "failed")

# Landed whole and waiting its turn: the board's LLM serves one meeting at a
# time, so a meeting sent while another is processed waits rather than being
# turned away.
QUEUED = "queued"

INTERRUPTED = (
    "processing stopped before it finished (the worker was stopped or "
    "restarted); upload it again"
)

SAMPLE_RATE = 16000
MB = 1024 * 1024

# Starts with a letter or digit, so "." and ".." are not ids; no slashes,
# so an id cannot name a folder outside meetings_dir.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def valid_id(meeting_id: str) -> bool:
    return bool(_ID.fullmatch(meeting_id))


def new_id(now: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now))


def free_mb(path: str) -> float:
    return shutil.disk_usage(path).free / MB


def check_audio(path: str) -> Optional[str]:
    """Why the file cannot be processed, or None. Only the header is read:
    an hour-long upload is judged before the client hangs up."""
    try:
        info = sf.info(path)
    except Exception:  # pylint: disable=broad-except
        return "not a readable audio file; send Ogg-Opus"
    if (info.format, info.subtype) != ("OGG", "OPUS"):
        return f"{info.format}/{info.subtype}; only Ogg-Opus is accepted"
    if info.samplerate != SAMPLE_RATE:
        return f"{info.samplerate} Hz; the models want {SAMPLE_RATE}"
    if info.channels != 1:
        return f"{info.channels} channels; mono only"
    return None


def land(upload: str, folder: str) -> Tuple[str, str]:
    """The upload becomes <folder>/audio.ogg, and whatever an earlier run of
    the same id left -- its record, its minutes, its work/ -- goes, so the
    status read next is this run's."""
    os.makedirs(folder, exist_ok=True)
    for name in ("record.json", "minutes.txt"):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            os.remove(path)
    work_dir = os.path.join(folder, "work")
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir)
    ogg_path = os.path.join(folder, "audio.ogg")
    os.replace(upload, ogg_path)
    return ogg_path, work_dir


def _state_path(folder: str) -> str:
    return os.path.join(folder, "work", "state.json")


def read_state(folder: str) -> Optional[dict]:
    try:
        with open(_state_path(folder), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(folder: str, state: dict) -> None:
    # Temporary file then rename: the controller and a reader of
    # GET /meeting/{id} never see half a file.
    work_dir = os.path.dirname(_state_path(folder))
    state["updated_at"] = time.time()
    fd, tmp = tempfile.mkstemp(dir=work_dir, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, _state_path(folder))


def write_queued(
    folder: str,
    meeting_id: str,
    details: dict,
    received_at: float,
    duration_s: float,
) -> None:
    """A landed meeting, waiting its turn: its state, and what came with it
    (title, speakers, recorded_at, script) for when it is handed on."""
    with open(os.path.join(folder, "upload.json"), "w", encoding="utf-8") as f:
        json.dump({**details, "duration_s": duration_s}, f, ensure_ascii=False)
    _write(
        folder,
        {
            "meeting_id": meeting_id,
            "state": QUEUED,
            "title": details.get("title"),
            "received_at": received_at,
            "topics_done": 0,
            "topics_total": 0,
            "error": None,
        },
    )


def read_upload(folder: str) -> dict:
    try:
        with open(os.path.join(folder, "upload.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def duration_s(path: str) -> float:
    return float(sf.info(path).duration)


def estimate_s(seconds: float) -> float:
    """About how long the board takes over a meeting this long: measured at
    0.93 to 1.1 times the recording, and the conclusion on top."""
    return seconds * 1.1 + 60.0


def queued(meetings_dir: str) -> List[Tuple[str, str, dict]]:
    """The meetings waiting their turn, first come first."""
    waiting = [
        m for m in _meetings(meetings_dir) if m[2].get("state") == QUEUED
    ]
    waiting.sort(key=lambda m: (m[2].get("received_at") or 0, m[0]))
    return waiting


def write_state(folder: str, **fields) -> None:
    """Merged into what is there, as the controller's own writes are."""
    _write(folder, {**(read_state(folder) or {}), **fields})


def status(folder: str, meeting_id: str) -> Optional[dict]:
    """GET /meeting/{id}, or None when there is no such meeting."""
    state = read_state(folder)
    if state is None:
        return None
    record = None
    record_path = os.path.join(folder, "record.json")
    if state.get("state") in TERMINAL and os.path.isfile(record_path):
        with open(record_path, encoding="utf-8") as f:
            record = json.load(f)
    return {
        "meeting_id": meeting_id,
        "state": state.get("state"),
        "progress": {
            "topics_done": state.get("topics_done", 0),
            "topics_total": state.get("topics_total", 0),
        },
        "record": record,
        "error": state.get("error"),
    }


def _meetings(meetings_dir: str):
    if not os.path.isdir(meetings_dir):
        return
    for name in sorted(os.listdir(meetings_dir)):
        folder = os.path.join(meetings_dir, name)
        if not (valid_id(name) and os.path.isdir(folder)):
            continue
        state = read_state(folder)
        if state is not None:
            yield name, folder, state


def list_meetings(meetings_dir: str) -> List[dict]:
    """GET /meetings: newest upload first."""
    out = [
        {
            "meeting_id": name,
            "state": state.get("state"),
            "title": state.get("title"),
            "received_at": state.get("received_at"),
        }
        for name, _, state in _meetings(meetings_dir)
    ]
    out.sort(key=lambda m: m["received_at"] or 0, reverse=True)
    return out


def fail_interrupted(meetings_dir: str) -> List[str]:
    """At start-up, once this worker holds the port, a meeting not left in
    a final state has no worker on it any more: only the one holding the
    port takes meetings. Say so on disk, rather than let its state read
    "transcribing" forever. Never called before the port is held. A queued
    meeting is left to wait: its upload is whole, and this worker will take
    it in turn."""
    stopped = []
    for name, folder, state in _meetings(meetings_dir):
        if state.get("state") not in TERMINAL + (QUEUED,):
            write_state(folder, state="failed", error=INTERRUPTED)
            stopped.append(name)
    return stopped
