#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""What the board's LLM writes for one meeting topic, and how long it takes.

  python3.12 tools/ambarella/probe_meeting_summary.py              # topic 1
  python3.12 tools/ambarella/probe_meeting_summary.py --topic 2
  python3.12 tools/ambarella/probe_meeting_summary.py --record path/record.json

On the board the meeting graph's summaries came back empty: the first
topic's answer began 2.8 s after a 1601-character prompt went in, was
still streaming at 175 s when the llm extension gave up, and the next
topic was refused because the daemon was still writing the first. This
asks the same question with no time limit and shows the whole answer.

  thinking   what tools/ambarella/set_llm_thinking.sh reports
  prompt     segment_prompt from meeting_control_python's own config.py,
             then the topic's lines in the record's format -- the same
             characters the graph sent, rebuilt from the last board check's
             record.json
  answer     streamed straight from the daemon, as ambarella_llm2_python
             reads it: data: events, <SP>/<NL> unescaped, <DONE> at the end
  report     time to the first character and to the end, characters and
             characters per second, how much came before </think>, whether
             lines repeat, the head and the tail

Run it with nothing else using the LLM: the daemon serves one session at a
time and refuses a second by closing the connection. Everything is also
saved under --out.
"""

import argparse
import codecs
import collections
import glob
import http.client
import importlib.util
import json
import os
import random
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ENV_FILE = os.path.join(REPO, "ai_agents", ".env")
CONTROL_CONFIG = os.path.join(
    REPO,
    "ai_agents",
    "agents",
    "ten_packages",
    "extension",
    "meeting_control_python",
    "config.py",
)
GIVE_UP_S = 175.0  # the meeting graph's llm total_timeout_s
DONE = ("<DONE>", "[DONE]")
THINK_END = "</think>"


def say(msg):
    print(f"\n== {msg}", flush=True)


def info(msg):
    print(f"        {msg}", flush=True)


def setting(key, default):
    if os.environ.get(key):
        return os.environ[key]
    if os.path.isfile(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def mmss(seconds):
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


# --------------------------------------------------------------------------


def thinking():
    say("thinking (tools/ambarella/set_llm_thinking.sh, reports only)")
    script = os.path.join(HERE, "set_llm_thinking.sh")
    try:
        out = subprocess.run(
            [script], capture_output=True, text=True, timeout=60, check=False
        )
        for line in (out.stdout + out.stderr).strip().splitlines()[-12:]:
            info(line)
    except (OSError, subprocess.TimeoutExpired) as err:
        info(f"could not run it: {err}")


def segment_prompt():
    """The shipped prompt, read from the controller's own config.py."""
    spec = importlib.util.spec_from_file_location("control_config", CONTROL_CONFIG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MeetingControlConfig().segment_prompt


def build_prompt(record_path, topic_no):
    with open(record_path, encoding="utf-8") as f:
        record = json.load(f)
    topics = record["topics"]
    if not 1 <= topic_no <= len(topics):
        sys.exit(f"{record_path} has {len(topics)} topics; --topic {topic_no}")
    topic = topics[topic_no - 1]
    if topic.get("error"):
        sys.exit(f"topic {topic_no} failed in that run: {topic['error']}")
    # record.py's lines_for(), for a record with no recorded_at.
    lines = [
        f"[{mmss(topic['start_s'] + u['start_s'])}] "
        f"說話人{u['speaker'] + 1}：{u['text']}"
        for u in topic["utterances"]
    ]
    return segment_prompt() + "\n".join(lines), topic


def ask(base_url, prompt, max_seconds, raw_path):
    """Stream one answer. Returns (raw text, first_s, total_s, how it ended)."""
    url = urllib.parse.urlparse(base_url)
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=120)
    headers = {
        # The daemon parses this as a number and refuses zero.
        "Session-Id": str(random.randint(100_000_000, 999_999_999)),
        "Model-Type": "9",
        "Stream-Off": "0",
        "Reset-En": "1",
        "Content-Type": "text/plain; charset=utf-8",
    }
    t0 = time.monotonic()
    conn.request("POST", url.path or "/", prompt.encode("utf-8"), headers)
    resp = conn.getresponse()
    info(f"HTTP {resp.status} after {time.monotonic() - t0:.1f} s")

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending, payloads = "", []
    first, ended, next_report = None, "connection closed", t0 + 15
    with open(raw_path, "w", encoding="utf-8") as raw:
        while True:
            if time.monotonic() - t0 > max_seconds:
                ended = f"stopped by this probe at {max_seconds:.0f} s"
                break
            chunk = resp.read1(4096)
            if not chunk:
                break
            text = decoder.decode(chunk)
            raw.write(text)
            pending += text
            *lines, pending = pending.split("\n")
            for line in lines:
                line = line.rstrip("\r")
                if not line.startswith("data:"):
                    continue
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                if value in DONE:
                    ended = value
                    break
                if first is None:
                    first = time.monotonic() - t0
                payloads.append(value)
            if ended in DONE:
                break
            now = time.monotonic()
            if now >= next_report:
                chars = len(unescape("".join(payloads)))
                info(f"{mmss(now - t0)}  {chars} characters so far")
                next_report = now + 15
    conn.close()
    return "".join(payloads), first, time.monotonic() - t0, ended


def unescape(text):
    return text.replace("<SP>", " ").replace("<NL>", "\n")


def repetition(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    counts = collections.Counter(lines)
    repeated = {l: n for l, n in counts.items() if n > 1}
    return lines, repeated


def report(answer, first, total, ended, prompt_chars):
    say("result")
    info(f"prompt {prompt_chars} characters")
    info(f"first character after {first:.1f} s" if first is not None
         else "no character at all")
    info(f"ended after {total:.1f} s ({ended})")
    chars = len(answer)
    speed = chars / max(total - (first or 0.0), 0.001)
    info(f"answer {chars} characters, {speed:.1f} per second while writing")
    if total > GIVE_UP_S:
        info(f"the meeting graph gives up at {GIVE_UP_S:.0f} s: this answer "
             f"would have been lost")
    else:
        info(f"within the graph's {GIVE_UP_S:.0f} s")

    cut = answer.find(THINK_END)
    if cut >= 0:
        info(f"{THINK_END} at character {cut}: {cut} characters of reasoning, "
             f"{chars - cut - len(THINK_END)} of answer after it")
    else:
        info(f"no {THINK_END}: no reasoning block (or thinking is bypassed)")

    lines, repeated = repetition(answer)
    if repeated:
        worst = max(repeated.items(), key=lambda kv: kv[1])
        info(f"{len(lines)} lines, {sum(repeated.values())} of them repeats; "
             f"most repeated ({worst[1]}x): {worst[0][:80]}")
    else:
        info(f"{len(lines)} lines, none repeated")

    say("answer, first 1500 characters")
    print(answer[:1500])
    if chars > 1500:
        say("answer, last 800 characters")
        print(answer[-800:])


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--record", help="record.json (default: the last board check)")
    p.add_argument("--topic", type=int, default=1)
    p.add_argument("--max-seconds", type=float, default=900.0)
    p.add_argument("--out", default="~/meeting_probe/summary_probe")
    args = p.parse_args()

    record = args.record or (sorted(glob.glob(os.path.expanduser(
        "~/meeting_probe/board_check/*/record.json"))) or [None])[-1]
    if not record:
        sys.exit("no record.json: run check_meeting_board.py first, or pass --record")
    out_dir = os.path.join(os.path.expanduser(args.out), time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    thinking()

    say(f"prompt: topic {args.topic} of {record}")
    prompt, topic = build_prompt(record, args.topic)
    with open(os.path.join(out_dir, "prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)
    info(f"{len(prompt)} characters, {len(topic['utterances'])} lines, "
         f"topic {mmss(topic['start_s'])}-{mmss(topic['end_s'])}")

    base_url = setting("AMBARELLA_LLM_BASE_URL", "http://127.0.0.1:8080")
    say(f"asking {base_url} (no limit but --max-seconds {args.max_seconds:.0f})")
    raw, first, total, ended = ask(
        base_url, prompt, args.max_seconds, os.path.join(out_dir, "raw.sse")
    )
    answer = unescape(raw)
    with open(os.path.join(out_dir, "answer.txt"), "w", encoding="utf-8") as f:
        f.write(answer)
    report(answer, first, total, ended, len(prompt))
    print(f"\nsaved prompt.txt, raw.sse, answer.txt in {out_dir}", flush=True)


if __name__ == "__main__":
    main()
