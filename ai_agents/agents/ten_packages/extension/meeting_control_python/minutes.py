#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""minutes.txt: the record as a person reads it, rendered from the same
MeetingRecord that record.json comes from, so the two never disagree."""

import time
from typing import List

from .record import MeetingRecord, _mmss


def render_minutes(
    record: MeetingRecord, summary: str, actions: List[dict]
) -> str:
    facts = []
    if record.recorded_at is not None:
        facts.append(
            time.strftime("%Y-%m-%d %H:%M", time.localtime(record.recorded_at))
        )
    facts.append(f"長度 {round(record.duration_s / 60)} 分鐘")
    facts.append(f"與會 {record.speaker_count} 人")
    lines = [record.title or record.meeting_id, " · ".join(facts), "", "結論"]
    lines.append(
        summary.strip() or "（整場結論未能產出；各話題的重點與逐字稿在下面）"
    )

    if actions:
        lines += ["", "待辦（盡力整理，可能不完整）"]
        for a in actions:
            who = f" —— {a['owner']}" if a.get("owner") else ""
            due = f"，{a['due_raw']}" if a.get("due_raw") else ""
            at = f"（{a['raised_at']} 提出）" if a.get("raised_at") else ""
            lines.append(f"- {a.get('what') or '（未寫明）'}{who}{due}{at}")

    for n, topic in enumerate(record.ordered(), 1):
        lines += ["", f"話題 {n} · {_mmss(topic.start_s)}–{_mmss(topic.end_s)}"]
        if topic.summary:
            lines += [topic.summary.strip(), ""]
        lines.append(record.lines_for(topic.id))
    return "\n".join(lines) + "\n"
