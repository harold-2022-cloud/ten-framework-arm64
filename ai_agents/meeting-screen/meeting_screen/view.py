#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""What the screen shows now: which meeting, in which scene, at which stage,
and whether its speaker numbers can be shown yet. Pure: the meetings and
the clock come in, the body of /api/view goes out; the page only draws it.

Auto mode, the showcase meeting left out:
  1. a meeting not finished (queued to concluding): the newest received;
  2. else the newest, if received within recent_min: its result or failure;
  3. else the ready screen.
A pin shows that meeting, the showcase included; the page lets go of it
when newest_received_at grows.
"""

from typing import Callable, List, Optional, Tuple

from .disk import Meeting

# The controller's states in order (meeting_control_python/flow.py), and the
# stage each lights: 0 queued, 1 received, 2 transcribing, 3 linking,
# 4 summarising, 5 concluding, 6 ended.
STAGE = {
    "queued": 0,
    "received": 1,
    "decoding": 1,
    "transcribing": 2,
    "linking": 3,
    "summarising": 4,
    "concluding": 5,
    "archived": 6,
    "failed": 6,
    "empty": 6,
}
TERMINAL = ("archived", "empty", "failed")
# Speaker numbers name one person across the meeting only once linked: the
# record is relabelled and saved (flow.py:179-180) before the state says
# summarising (flow.py:183); before, each topic numbers its own speakers.
SPEAKERS_FINAL = ("summarising", "concluding", "archived")


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _state(m: Meeting) -> Optional[str]:
    value = m.state.get("state")
    return value if isinstance(value, str) else None


def _key(m: Meeting) -> Tuple[float, str]:
    return (m.received_at, m.meeting_id)


def scene_of(state: Optional[str]) -> str:
    if state == "queued":
        return "queued"
    if state == "archived":
        return "result"
    if state in ("failed", "empty"):
        return "failed"
    return "processing"


def choose(
    meetings: List[Meeting],
    now: float,
    showcase: Optional[str] = None,
    pin: Optional[str] = None,
    recent_min: float = 60.0,
) -> Tuple[Optional[Meeting], bool]:
    """The meeting to show, and whether it is pinned."""
    if pin is not None:
        for m in meetings:
            if m.meeting_id == pin:
                return m, True
    live = [m for m in meetings if m.meeting_id != showcase]
    going = [m for m in live if _state(m) not in TERMINAL]
    if going:
        return max(going, key=_key), False
    if live:
        newest = max(live, key=_key)
        if now - newest.received_at <= recent_min * 60:
            return newest, False
    return None, False


def ahead_of(
    m: Meeting, meetings: List[Meeting], showcase: Optional[str]
) -> int:
    """A queued meeting's place, as the uploader counts it (queue_of): the
    queued ones received before it, and one more if one is processed."""
    others = [
        o for o in meetings if o.meeting_id not in (m.meeting_id, showcase)
    ]
    before = [o for o in others if _state(o) == "queued" and _key(o) < _key(m)]
    busy = any(_state(o) not in TERMINAL + ("queued",) for o in others)
    return len(before) + (1 if busy else 0)


def meeting_view(
    m: Meeting,
    record,
    meetings: List[Meeting],
    now: float,
    showcase: Optional[str],
) -> dict:
    state = _state(m)
    started = _num(m.state.get("started_at"))
    finished = _num(m.state.get("updated_at")) if state in TERMINAL else None
    if started is None:
        elapsed = None
    elif finished is not None:
        elapsed = max(0.0, finished - started)
    else:
        elapsed = max(0.0, now - started)
    record = record if isinstance(record, dict) else None
    from_record = record or {}
    title = (
        m.state.get("title")
        or m.upload.get("title")
        or from_record.get("title")
        or ""
    )
    duration = (
        _num(m.upload.get("duration_s"))
        or _num(from_record.get("duration_s"))
        or 0.0
    )
    return {
        "meeting_id": m.meeting_id,
        "title": title,
        "state": state,
        "stage": STAGE.get(state, 1),
        "error": m.state.get("error"),
        "received_at": _num(m.state.get("received_at")),
        "started_at": started,
        "finished_at": finished,
        "elapsed_s": elapsed,
        "duration_s": duration,
        "progress": {
            "topics_done": int(_num(m.state.get("topics_done")) or 0),
            "topics_total": int(_num(m.state.get("topics_total")) or 0),
        },
        "ahead": ahead_of(m, meetings, showcase) if state == "queued" else 0,
        "speakers_final": state in SPEAKERS_FINAL,
        "showcase": m.meeting_id == showcase,
        "record": record,
    }


def build(
    meetings: List[Meeting],
    read_record: Callable[[str], Optional[dict]],
    now: float,
    showcase: Optional[str] = None,
    pin: Optional[str] = None,
    recent_min: float = 60.0,
) -> dict:
    """The body of GET /api/view."""
    chosen, pinned = choose(meetings, now, showcase, pin, recent_min)
    live = [m.received_at for m in meetings if m.meeting_id != showcase]
    on_disk = any(m.meeting_id == showcase for m in meetings)
    return {
        "now": now,
        "newest_received_at": max(live) if live else None,
        "scene": "ready" if chosen is None else scene_of(_state(chosen)),
        "pinned": pinned,
        "showcase_id": showcase if on_disk else None,
        "meeting": (
            None
            if chosen is None
            else meeting_view(
                chosen, read_record(chosen.meeting_id), meetings, now, showcase
            )
        ),
    }


def listing(
    meetings: List[Meeting], showcase: Optional[str] = None
) -> List[dict]:
    """The body of GET /api/meetings: the newest received first."""
    return [
        {
            "meeting_id": m.meeting_id,
            "title": m.state.get("title") or m.upload.get("title") or "",
            "state": _state(m),
            "received_at": _num(m.state.get("received_at")),
            "duration_s": _num(m.upload.get("duration_s")),
            "showcase": m.meeting_id == showcase,
        }
        for m in sorted(meetings, key=_key, reverse=True)
    ]
