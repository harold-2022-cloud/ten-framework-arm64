#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Watch the board while a phone sends it a meeting, and say whether the
app and the board each did their part.

  python3.12 tools/ambarella/check_meeting_phone.py
  python3.12 tools/ambarella/check_meeting_phone.py --script simplified

Start it, then on the phone (the Android app, board address set):
開始錄音, talk for a minute or two with someone else, 散會，結束錄音,
fill in the attendees, 上傳到會議室的板子. It reads the meeting off the
board's disk, so it never holds 8765 or gets in the app's way. In order:

  arrive    a new meeting lands under MEETINGS_DIR within --wait-minutes
  channel   the worker that holds it runs on channel meeting-room, the
            app's one channel for every meeting
  process   its state, followed to the end
  record    archived, with topics, a conclusion, no U+FFFD, and every text
            in --script (the app's default is traditional)
  left      the worker still answers 30 s after the meeting ended: the app
            never stops it; the board reaps it ten minutes later

Exits non-zero if any check failed.
"""

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# pylint: disable=wrong-import-position
import check_meeting_board as board
from check_meeting_board import FINAL, GONE, info, mmss, ok, request, say, setting, warn

CHANNEL = "meeting-room"  # the app's: domain/Small.kt Ids.CHANNEL
# A worker's property file, named for its channel, on its command line.
PROPERTY = re.compile(r"property-(meeting.*?)-\d{8}_\d{6}_\d+\.json")


def fail(msg, fix=None):
    board.fail(msg, fix)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def worker_channels():
    """The channels of the meeting workers running now."""
    found = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode(errors="ignore")
        except OSError:
            continue
        found.update(PROPERTY.findall(cmdline))
    return found


def arrive(args, meetings_dir):
    say(f"arrive: waiting up to {args.wait_minutes:g} minutes for the phone")
    before = set(os.listdir(meetings_dir)) if os.path.isdir(meetings_dir) else set()
    info("on the phone: 開始錄音, a minute or two of two people talking,")
    info("散會，結束錄音, the attendees, then 上傳到會議室的板子")
    print("\a", end="", flush=True)
    t0 = time.monotonic()
    shown = 0
    while time.monotonic() - t0 < args.wait_minutes * 60:
        if os.path.isdir(meetings_dir):
            for name in sorted(set(os.listdir(meetings_dir)) - before):
                folder = os.path.join(meetings_dir, name)
                if os.path.isfile(os.path.join(folder, "work", "state.json")):
                    return name, folder
        waited = time.monotonic() - t0
        if waited - shown >= 60:
            shown = waited
            info(f"still waiting ({mmss(waited)})")
        time.sleep(2)
    fail(f"no meeting arrived under {meetings_dir} in {args.wait_minutes:g} minutes",
         "is the app's board address this board? its 設定 page has it")
    return None, None


def run(args, env):
    meetings_dir = os.path.expanduser(setting(env, "MEETINGS_DIR", "/tmp/meetings"))
    meeting_id, folder = arrive(args, meetings_dir)
    if not meeting_id:
        return
    state = read_json(os.path.join(folder, "work", "state.json")) or {}
    ok(f"meeting {meeting_id} arrived" + (f" ({state['title']})" if state.get("title") else ""))

    say("channel")
    channels = worker_channels()
    if CHANNEL in channels:
        ok(f"the worker runs on channel {CHANNEL}")
    elif channels:
        fail(f"the worker runs on {', '.join(sorted(channels))}, not {CHANNEL}",
             "an app from before the shared worker: install the current APK")
    else:
        warn("no meeting worker process seen")

    say("process")
    t0 = time.monotonic()
    last = None
    while time.monotonic() - t0 < args.max_minutes * 60:
        state = read_json(os.path.join(folder, "work", "state.json")) or {}
        now = (state.get("state"), state.get("topics_done"))
        if now != last and state.get("state"):
            print(f"  {mmss(time.monotonic() - t0)}  {state.get('state'):<13}"
                  f"{state.get('topics_done')}/{state.get('topics_total')}", flush=True)
            last = now
        if state.get("state") in FINAL:
            break
        time.sleep(5)
    else:
        fail(f"not finished after {args.max_minutes:g} minutes")
        return
    t_end = time.monotonic()

    say("record")
    final = state.get("state")
    record = read_json(os.path.join(folder, "record.json")) or {}
    if final == "archived":
        ok("archived")
    elif final == "empty":
        fail("ended empty: the board heard no speech in the recording",
             "talk closer to the phone, two people, a minute or more")
    else:
        fail(f"ended {final}: {state.get('error')}")
    topics = record.get("topics") or []
    lines = sum(len(t.get("utterances") or []) for t in topics)
    speakers = {u.get("speaker") for t in topics for u in t.get("utterances") or []}
    info(f"recording {mmss(record.get('duration_s') or 0)}, {len(topics)} topic(s), "
         f"{lines} line(s), {len(speakers)} speaker(s) heard")
    if final == "archived":
        if topics and lines:
            ok("the transcript has lines")
        else:
            fail("no transcript lines")
        if record.get("summary"):
            ok(f"conclusion, {len(record['summary'])} characters")
        else:
            fail("no conclusion")
        minutes = os.path.join(folder, "minutes.txt")
        if os.path.isfile(minutes):
            with open(minutes, encoding="utf-8") as f:
                broken = sum("�" in line for line in f)
            if broken:
                fail(f"minutes.txt: {broken} line(s) with U+FFFD")
            else:
                ok("no U+FFFD")
        board.check_script(record, args.script)

    say("left: the app does not stop the worker")
    while time.monotonic() - t_end < 30:
        time.sleep(2)
    if request(f"{args.uploader}/meetings", timeout=3)[0] != GONE:
        ok(f"the worker still answers 30 s after the meeting ended; "
           "the board reaps it about ten minutes after")
    else:
        fail("the worker went within 30 s of the meeting ending: something "
             "stopped it",
             "an app from before the shared worker stops it once it has the "
             "record: install the current APK")
    info(f"meeting folder {folder}")
    info("on the phone: the meeting should read 完成; open it, rename a "
         "speaker, share it")


def main():
    env = board.dotenv()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--script", choices=("traditional", "simplified"),
                   default="traditional",
                   help="what the app was set to send (its default: traditional)")
    p.add_argument("--wait-minutes", type=float, default=20.0,
                   help="how long to wait for the phone's upload")
    p.add_argument("--max-minutes", type=float, default=30.0,
                   help="how long the processing may take")
    p.add_argument("--uploader", default="http://127.0.0.1:8765")
    args = p.parse_args()

    run(args, env)

    print(flush=True)
    if board.failures:
        print(f"{len(board.failures)} check(s) failed:", flush=True)
        for f in board.failures:
            print(f"  - {f}", flush=True)
        sys.exit(1)
    print("all checks passed", flush=True)


if __name__ == "__main__":
    main()
