#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A meeting's record.json as one HTML page to read, keep or print: the
conclusion first (its numbered items as a list), how much each speaker said
and when, then every topic with its summary and, folded, its transcript.

  python3 tools/ambarella/minutes_html.py meetings/<id>/record.json
  python3 tools/ambarella/minutes_html.py record.json -o somewhere.html

Writes minutes.html beside the record unless told otherwise. The page is
self-contained -- its style inline, no script, nothing loaded from anywhere --
so it opens offline and stays on this machine: a meeting's words are the
people's who said them. upload_meeting.py writes one with every result it
brings back. Standard library only.
"""

import argparse
import html
import json
import os
import re
import sys
import time

# Speaker n always the n-th, as on the board's screen (screen_model.mjs).
COLORS = [
    "#1f77b4",
    "#e8833a",
    "#2ca02c",
    "#d62f7a",
    "#8a63d2",
    "#17a2a8",
    "#b8a200",
    "#d9534f",
    "#4f8fd6",
    "#a35bb4",
]
HEADING = re.compile(r"^[一二三四五六七八九十]+、\s*(.+)$")
# "1. ...", not "1.5 hours" or "2026.10.20": one or two digits, no digit after.
ITEM = re.compile(r"^\d{1,2}[.、．](?!\d)\s*(.+)$")

STYLE = """
:root { color-scheme: light dark; --bg: #f7f8fb; --card: #fff; --text: #1d2433;
  --muted: #667085; --line: #e4e7ee; --accent: #2457c5; --bad: #c0392b; }
@media (prefers-color-scheme: dark) { :root { --bg: #0f1522; --card: #172033;
  --text: #e7ecf5; --muted: #98a2b8; --line: #263147; --accent: #7aa7ff; --bad: #ff7b6b; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 17px/1.75
  "Noto Sans CJK TC", "PingFang TC", "Microsoft JhengHei", sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; }
header h1 { margin: 0 0 8px; font-size: 30px; line-height: 1.3; }
.facts { display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; list-style: none; }
.facts li { padding: 2px 12px; border: 1px solid var(--line); border-radius: 999px;
  background: var(--card); color: var(--muted); font-size: 14px; }
section { margin-top: 24px; padding: 24px 28px; border: 1px solid var(--line);
  border-radius: 14px; background: var(--card); }
section > h2 { margin: 0 0 12px; font-size: 21px; }
.conclusion { border-left: 5px solid var(--accent); }
.conclusion h3 { margin: 18px 0 6px; font-size: 18px; color: var(--accent); }
.conclusion h3:first-child { margin-top: 0; }
.conclusion p, .conclusion li { font-size: 18px; }
.conclusion ol { margin: 0; padding-left: 1.6em; }
.speakers { display: grid; grid-template-columns: 7em 1fr 4.5em; gap: 6px 12px; align-items: center; }
.who { font-weight: 700; white-space: nowrap; }
.bar { height: 10px; border-radius: 5px; background: var(--line); overflow: hidden; }
.bar i, .lane i { display: block; height: 100%; }
.share { color: var(--muted); font-size: 14px; text-align: right; font-variant-numeric: tabular-nums; }
.lanes { margin-top: 18px; display: grid; grid-template-columns: 7em 1fr; gap: 4px 12px; align-items: center; }
.lane { position: relative; height: 8px; border-radius: 4px; background: var(--line); }
.lane i { position: absolute; top: 0; border-radius: 2px; }
.scale { display: flex; justify-content: space-between; grid-column: 2; color: var(--muted); font-size: 12px; }
.topic h2 { display: flex; justify-content: space-between; gap: 12px; }
.topic .span { color: var(--muted); font-size: 15px; font-weight: 400; font-variant-numeric: tabular-nums; }
.error { color: var(--bad); }
details { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 10px; }
summary { cursor: pointer; color: var(--accent); }
.row { display: grid; grid-template-columns: 4.2em 6em 1fr; gap: 10px; padding: 3px 0; font-size: 15px; }
.row .at { color: var(--muted); font-variant-numeric: tabular-nums; }
footer { margin-top: 28px; color: var(--muted); font-size: 13px; text-align: center; }
@media print { body { background: #fff; } section { break-inside: avoid-page; } }
@media (max-width: 640px) { .row { grid-template-columns: 3.6em 1fr; } .row .who { grid-column: 2; }
  .row .text { grid-column: 1 / -1; } section { padding: 18px; } }
"""


def esc(text):
    return html.escape(str(text or ""), quote=True)


def mmss(seconds):
    s = max(0, int(seconds or 0))
    return f"{s // 60:02d}:{s % 60:02d}"


def color(speaker):
    return COLORS[int(speaker) % len(COLORS)]


def blocks(summary):
    """The conclusion's lines as headings, numbered items and paragraphs."""
    out = []
    for line in (summary or "").splitlines():
        line = line.strip()
        if not line:
            continue
        head, item = HEADING.match(line), ITEM.match(line)
        if head:
            out.append(("heading", head.group(1)))
        elif item:
            out.append(("item", item.group(1)))
        else:
            out.append(("para", line))
    return out


def conclusion_html(summary):
    parts, items = [], []

    def flush():
        if items:
            parts.append("<ol>\n" + "\n".join(f"<li>{esc(i)}</li>" for i in items) + "\n</ol>")
            items.clear()

    for kind, text in blocks(summary):
        if kind == "item":
            items.append(text)
            continue
        flush()
        parts.append(f"<h3>{esc(text)}</h3>" if kind == "heading" else f"<p>{esc(text)}</p>")
    flush()
    return "\n".join(parts) or "<p>（整場結論未能產出；各話題的重點與逐字稿在下面）</p>"


def turns(record):
    """(start in the meeting, end, speaker, text) for every utterance."""
    out = []
    for topic in record.get("topics") or []:
        base = float(topic.get("start_s") or 0.0)
        for u in topic.get("utterances") or []:
            if isinstance(u.get("speaker"), int):
                out.append((base + float(u.get("start_s") or 0), base + float(u.get("end_s") or 0),
                            u["speaker"], u.get("text") or ""))
    return out


def speakers_html(record):
    said = {}
    for start, end, speaker, _ in turns(record):
        said[speaker] = said.get(speaker, 0.0) + max(0.0, end - start)
    if not said:
        return ""
    whole = sum(said.values()) or 1.0
    rows = []
    for speaker, seconds in sorted(said.items(), key=lambda kv: -kv[1]):
        share = 100.0 * seconds / whole
        rows.append(
            f'<span class="who" style="color:{color(speaker)}">說話人 {speaker + 1}</span>'
            f'<div class="bar"><i style="width:{share:.1f}%;background:{color(speaker)}"></i></div>'
            f'<span class="share">{share:.1f}%</span>'
        )
    # The meeting's length, or past it if a line ends later; never 0.
    length = max([float(record.get("duration_s") or 0)] + [e for _, e, _, _ in turns(record)]) or 1.0
    lanes = []
    for speaker in sorted(said):
        marks = "".join(
            f'<i style="left:{100 * s / length:.2f}%;width:{max(0.15, 100 * (e - s) / length):.2f}%;'
            f'background:{color(speaker)}"></i>'
            for s, e, who, _ in turns(record) if who == speaker
        )
        lanes.append(f'<span class="who" style="color:{color(speaker)}">說話人 {speaker + 1}</span>'
                     f'<div class="lane">{marks}</div>')
    scale = f'<div class="scale"><span>00:00</span><span>{mmss(length)}</span></div>'
    return ('<section><h2>誰說了多少、什麼時候</h2><div class="speakers">' + "".join(rows)
            + '</div><div class="lanes">' + "".join(lanes) + scale + "</div></section>")


def topics_html(record):
    out = []
    for n, topic in enumerate(record.get("topics") or [], 1):
        start, end = float(topic.get("start_s") or 0), float(topic.get("end_s") or 0)
        body = []
        if topic.get("error"):
            body.append(f'<p class="error">這個話題沒有處理完成：{esc(topic["error"])}</p>')
        if topic.get("summary"):
            body.append(f"<p>{esc(topic['summary'])}</p>")
        # In the meeting, as minutes.txt times it: the topic's start plus
        # the line's own (record.py lines_for).
        rows = [
            f'<div class="row"><span class="at">[{mmss(start + float(u.get("start_s") or 0))}]</span>'
            + (f'<span class="who" style="color:{color(u["speaker"])}">說話人 {u["speaker"] + 1}</span>'
               if isinstance(u.get("speaker"), int) else "<span></span>")
            + f'<span class="text">{esc(u.get("text"))}</span></div>'
            for u in topic.get("utterances") or []
        ]
        if rows:
            body.append(f"<details><summary>逐字稿（{len(rows)} 句）</summary>{''.join(rows)}</details>")
        out.append(f'<section class="topic"><h2><span>話題 {n} · {mmss(start)}–{mmss(end)}</span>'
                   f'<span class="span">{mmss(end - start)}</span></h2>{"".join(body)}</section>')
    return "".join(out)


def render(record):
    title = record.get("title") or record.get("meeting_id") or "會議紀錄"
    facts = []
    if isinstance(record.get("recorded_at"), (int, float)):
        facts.append(time.strftime("%Y-%m-%d %H:%M", time.localtime(record["recorded_at"])))
    facts.append(f"長度 {round(float(record.get('duration_s') or 0) / 60)} 分鐘")
    facts.append(f"發言 {record.get('speaker_count') or 0} 人")
    facts.append(f"話題 {len(record.get('topics') or [])} 個")
    if record.get("meeting_id"):
        facts.append(f"編號 {record['meeting_id']}")
    failed = ""
    if record.get("error"):
        failed = f'<p class="error">這場會議沒有處理完成：{esc(record["error"])}</p>'
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<header><h1>{esc(title)}</h1><ul class="facts">{"".join(f"<li>{esc(f)}</li>" for f in facts)}</ul>{failed}</header>
<section class="conclusion"><h2>結論</h2>
{conclusion_html(record.get("summary"))}
</section>
{speakers_html(record)}
{topics_html(record)}
<footer>N1-655 本機會議 AI 產生 · {time.strftime("%Y-%m-%d %H:%M")} · 這份檔案不連任何外部資源</footer>
</main>
</body>
</html>
"""


def write(record_path, out=None):
    with open(record_path, encoding="utf-8") as f:
        record = json.load(f)
    out = out or os.path.join(os.path.dirname(os.path.abspath(record_path)), "minutes.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(record))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("record", help="a meeting's record.json")
    p.add_argument("-o", "--out", help="the page (minutes.html beside the record)")
    args = p.parse_args(argv)
    print(write(args.record, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
