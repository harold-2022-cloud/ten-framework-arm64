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
# stage each lights: 0 queued, 1 received, 2 telling the speakers apart,
# 3 recognising their words (transcribing at step recognising), 4 linking,
# 5 summarising, 6 concluding, 7 ended.
STAGE = {
    "queued": 0,
    "received": 1,
    "decoding": 1,
    "transcribing": 2,
    "linking": 4,
    "summarising": 5,
    "concluding": 6,
    "archived": 7,
    "failed": 7,
    "empty": 7,
}
TERMINAL = ("archived", "empty", "failed")
# Speaker numbers name one person across the meeting only once linked: the
# record is relabelled and saved (flow.py:179-180) before the state says
# summarising (flow.py:183); before, each topic numbers its own speakers.
SPEAKERS_FINAL = ("summarising", "concluding", "archived")
# What an archived meeting with no words says when no topic says why.
NO_WORDS = "沒有轉出任何文字"


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _state(m: Meeting) -> Optional[str]:
    value = m.state.get("state")
    return value if isinstance(value, str) else None


def _key(m: Meeting) -> Tuple[float, str]:
    return (m.received_at, m.meeting_id)


def _wordless(state: Optional[str], record) -> bool:
    """Archived with a record but not one utterance: every topic's
    transcription failed (flow.py marks each failed, then links nothing and
    concludes). It ended, but it is no result."""
    if state != "archived" or not isinstance(record, dict):
        return False
    topics = record.get("topics")
    return not any(
        isinstance(t, dict) and t.get("utterances")
        for t in (topics if isinstance(topics, list) else [])
    )


def _topic_error(record) -> Optional[str]:
    topics = record.get("topics")
    for t in topics if isinstance(topics, list) else []:
        if isinstance(t, dict) and t.get("error"):
            return str(t["error"])
    return None


def stage_of(state: Optional[str], step: Optional[str]) -> int:
    """A topic is told apart first, then recognised: two stages in one
    state, told by the controller's step."""
    if state == "transcribing" and step == "recognising":
        return 3
    return STAGE.get(state, 1)


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
    step = m.state.get("step") if state == "transcribing" else None
    step = step if isinstance(step, str) else None
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
    error = m.state.get("error")
    if _wordless(state, record):
        error = error or _topic_error(record) or NO_WORDS
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
        "stage": stage_of(state, step),
        "step": step,
        "step_started_at": (
            _num(m.state.get("step_started_at")) if step else None
        ),
        "topic_s": _num(m.state.get("topic_s")) if step else None,
        "turns": {
            "done": int(_num(m.state.get("turns_done")) or 0),
            "total": int(_num(m.state.get("turns_total")) or 0),
        },
        "since": _num(m.state.get("updated_at")),
        "error": error,
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
    if chosen is None:
        scene, shown = "ready", None
    else:
        record = read_record(chosen.meeting_id)
        scene = scene_of(_state(chosen))
        if _wordless(_state(chosen), record):
            scene = "failed"
        shown = meeting_view(chosen, record, meetings, now, showcase)
    return {
        "now": now,
        "newest_received_at": max(live) if live else None,
        "scene": scene,
        "pinned": pinned,
        "showcase_id": showcase if on_disk else None,
        "meeting": shown,
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
