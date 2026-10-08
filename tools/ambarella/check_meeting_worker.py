#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Check on the board that every meeting can share the one meeting worker
without one disturbing another -- what the Android app relies on.

  python3.12 tools/ambarella/check_meeting_worker.py
  python3.12 tools/ambarella/check_meeting_worker.py --minutes 3

Takes about the --minutes of audio, plus two minutes. In order:

  checkout  the uploader fix is in this tree; stops here if not
  preflight the server answers, and no meeting worker is running yet
  start     /start meeting-room answers "0"; a second /start "10003"; /ping
            "0" -- the app's path when the worker is already up
  upload    the first --minutes of the AISHELL-4 meeting, as meeting A
  intruder  while A is processed, /start a second meeting worker on another
            channel. Its uploader cannot get 8765 and must leave A alone:
            before the fix, A read "failed" the moment it started. The
            intruder is this check's own, and is stopped again
  busy      a second upload while A is processed gets 409 naming A
  finish    A ends archived
  reaped    nobody pings once A is done: the worker is gone within its
            60 s timeout
  revive    /start meeting-room again; A still reads archived, with its
            record -- how the app reads a meeting after its worker went

It never stops meeting-room, as the app never does: the revived worker is
reaped a minute after the check ends. Exits non-zero if any check failed.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# pylint: disable=wrong-import-position
import check_meeting_board as board
from check_meeting_board import (
    FINAL,
    GONE,
    GRAPH,
    info,
    mmss,
    multipart,
    ok,
    post_json,
    request,
    say,
    setting,
    warn,
)

REPO = board.REPO
CHANNEL = "meeting-room"  # the app's: domain/Small.kt Ids.CHANNEL
FIX = "mark meetings interrupted only once the port is held"
SERVER_LOG = "/tmp/task_run.log"
NO_PORT = "cannot listen on"  # the uploader's line when 8765 is taken


def fail(msg, fix=None):
    board.fail(msg, fix)


def call(args, path, channel, **extra):
    """(HTTP status, the server's code) for a /start, /ping or /stop."""
    status, reply = post_json(
        f"{args.server}{path}",
        {"request_id": uuid.uuid4().hex, "channel_name": channel, **extra},
    )
    return status, (reply or {}).get("code")


def start(args, channel):
    return call(args, "/start", channel, graph_name=GRAPH, timeout=args.timeout)


def wait_ready(args, auth, within=90):
    t0 = time.monotonic()
    while request(f"{args.uploader}/meetings", headers=auth, timeout=3)[0] != 200:
        if time.monotonic() - t0 > within:
            return None
        time.sleep(1)
    return time.monotonic() - t0


def state_of(args, meeting_id, auth):
    status, reply = request(
        f"{args.uploader}/meeting/{meeting_id}", headers=auth, timeout=10
    )
    if status != 200:
        return None
    return json.loads(reply)


def upload(args, meeting_id, path, auth):
    fields = {
        "meeting_id": meeting_id,
        "title": "shared worker check",
        "speakers": str(args.speakers),
    }
    body, kind = multipart(fields, path)
    status, reply = request(
        f"{args.uploader}/meeting/upload",
        body,
        {"Content-Type": kind, **auth},
        timeout=120,
    )
    return status, reply.decode(errors="replace")


def log_count(needle):
    """How many lines of the server's log carry needle; None without it."""
    if not os.path.isfile(SERVER_LOG):
        return None
    with open(SERVER_LOG, encoding="utf-8", errors="replace") as f:
        return sum(needle in line for line in f)


# --------------------------------------------------------------------------


def checkout():
    say("checkout")
    found = subprocess.run(
        ["git", "-C", REPO, "log", "-1", "--format=%h %s", "--fixed-strings",
         f"--grep={FIX}", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    if not found:
        fail(f"this checkout lacks \"{FIX}\"",
             f"git -C {REPO} pull --ff-only; everything after would test "
             "the old uploader")
        return False
    ok(f"present: {found}")
    return True


def preflight(args, auth):
    say("preflight")
    status, _ = request(f"{args.server}/graphs", timeout=5)
    if status != 200:
        fail(f"no Go server at {args.server}",
             "start it: cd ai_agents/agents/examples/voice-assistant && task run")
        return False
    ok(f"{args.server} answers")
    if request(f"{args.uploader}/meetings", headers=auth, timeout=3)[0] != GONE:
        fail(f"{args.uploader} already answers: a meeting worker is running",
             "this check needs to start it, so its timeout is known. The app's "
             "worker is reaped ten minutes after its last meeting; wait, or "
             "see what it is doing: curl -s "
             f"{args.uploader}/meetings")
        return False
    ok(f"{args.uploader} is free")
    return True


def run(args, env):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.expanduser(os.path.join(args.out, stamp))
    os.makedirs(out_dir, exist_ok=True)
    token = args.token or setting(env, "MEETING_AUTH_TOKEN", "")
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    a_id, b_id = f"shared-{stamp}-a", f"shared-{stamp}-b"

    if not preflight(args, auth):
        return

    say("audio")
    audio, duration = board.prepare_audio(args, out_dir)
    ok(f"{audio}: {mmss(duration)} of 16 kHz mono Ogg-Opus")

    say(f"start (channel {CHANNEL}, timeout {args.timeout} s)")
    status, code = start(args, CHANNEL)
    if code != "0":
        fail(f"/start answered {status} code {code}")
        return
    ok('/start code "0"')
    status, code = start(args, CHANNEL)
    if code == "10003":
        ok('a second /start: code "10003", the worker is already running')
    else:
        fail(f'a second /start answered {status} code {code}, not "10003"')
    status, code = call(args, "/ping", CHANNEL)
    if code == "0":
        ok('/ping code "0": its idle clock restarts')
    else:
        fail(f"/ping answered {status} code {code}")
    took = wait_ready(args, auth)
    if took is None:
        fail(f"{args.uploader} did not answer within 90 s of /start",
             f"look for meeting_uploader in {SERVER_LOG}")
        return
    ok(f"uploader answered {took:.0f} s after /start")

    say(f"upload meeting A ({a_id})")
    status, reply = upload(args, a_id, audio, auth)
    if status != 200:
        fail(f"upload answered {status} {reply[:300]}")
        return
    ok(f"upload 200 {reply}")
    t_upload = time.monotonic()

    say("intruder: a second meeting worker while A is processed")
    t0 = time.monotonic()
    state = state_of(args, a_id, auth) or {}
    while state.get("state") in (None, "received", "decoding"):
        if time.monotonic() - t0 > 180:
            break
        time.sleep(2)
        state = state_of(args, a_id, auth) or {}
    if state.get("state") in FINAL:
        fail(f"A ended {state.get('state')} before the intruder could start; "
             "use a longer --minutes")
        return
    ok(f"A is {state.get('state')}")
    intruder = f"meeting-check-intruder-{stamp}"
    before = log_count(NO_PORT)
    status, code = start(args, intruder)
    if code != "0":
        fail(f"/start {intruder} answered {status} code {code}")
        return
    ok(f"/start {intruder}: a second worker is starting")
    seen, confirmed, t0 = [], before is None, time.monotonic()
    # Before the fix, the intruder's uploader marked A failed before it
    # tried the port, so A reads "failed" by the time the log says so.
    # Keep looking a little after that line, and at most 120 s without it.
    settle = None
    while time.monotonic() - t0 < 120:
        state = state_of(args, a_id, auth) or {}
        if state.get("state") and (not seen or seen[-1] != state["state"]):
            seen.append(state["state"])
        if before is not None and not confirmed and log_count(NO_PORT) > before:
            confirmed, settle = True, time.monotonic()
            ok(f"the intruder's uploader could not get the port "
               f"({time.monotonic() - t0:.0f} s; {SERVER_LOG})")
        if settle is not None and time.monotonic() - settle > 15:
            break
        if before is None and time.monotonic() - t0 > 60:
            break
        time.sleep(2)
    info(f"intruder worker {board.worker_rss_mb(intruder):.0f} MB")
    info(f"A's states meanwhile: {' -> '.join(seen) or 'none read'}")
    if "failed" in seen:
        fail("A read \"failed\" while the intruder started: the second "
             "worker marked a meeting it does not hold",
             "the workers do not load this checkout's uploader: the tenapp's "
             "ten_packages/extension/meeting_uploader should be a link to "
             "ai_agents/agents/ten_packages/extension/meeting_uploader "
             "(ls -la it); re-run install_board_arm64.sh if it is a copy")
    else:
        ok("A was never marked failed")
    if before is None:
        warn(f"no {SERVER_LOG}: could not see the intruder's uploader give "
             "up the port; the check above waited 60 s for it")
    elif not confirmed:
        fail("the intruder's uploader never said it could not get the port "
             "within 120 s; whether it would touch A was not tested")
    status, code = call(args, "/stop", intruder)
    if code == "0":
        ok(f"/stop {intruder} (this check's own worker)")
    else:
        warn(f"/stop {intruder} answered {status} code {code}")

    say(f"busy: meeting B ({b_id}) while A is processed")
    state = state_of(args, a_id, auth) or {}
    if state.get("state") in FINAL:
        warn("A had already ended; the 409 was not tested. Use a longer --minutes")
    else:
        status, reply = upload(args, b_id, audio, auth)
        if status == 409 and a_id in reply:
            ok(f"409 {reply}")
        else:
            fail(f"a second upload answered {status} {reply[:200]}, "
                 f"not 409 naming {a_id}")

    say("finish")
    deadline = t_upload + args.max_minutes * 60
    last = None
    while time.monotonic() < deadline:
        state = state_of(args, a_id, auth)
        if state is None:
            fail("the worker stopped answering while A was processed")
            return
        progress = state.get("progress") or {}
        now = (state.get("state"), progress.get("topics_done"))
        if now != last:
            print(f"  {mmss(time.monotonic() - t_upload)}  "
                  f"{state.get('state'):<13}{progress.get('topics_done')}/"
                  f"{progress.get('topics_total')}", flush=True)
            last = now
        if state.get("state") in FINAL:
            break
        time.sleep(10)
    else:
        fail(f"A not finished after {args.max_minutes} minutes")
        return
    t_done = time.monotonic()
    if state.get("state") == "archived":
        ok(f"A archived, {len((state.get('record') or {}).get('topics') or [])} topics")
    else:
        fail(f"A ended {state.get('state')}: {state.get('error')}")
        return

    say(f"reaped: nobody pings, timeout {args.timeout} s")
    while request(f"{args.uploader}/meetings", headers=auth, timeout=3)[0] != GONE:
        if time.monotonic() - t_done > args.timeout + 90:
            fail(f"the worker still answers {time.monotonic() - t_done:.0f} s "
                 f"after A ended; is something else pinging {CHANNEL}?")
            return
        time.sleep(5)
    ok(f"the worker went {time.monotonic() - t_done:.0f} s after A ended")

    say("revive: read A from a fresh worker")
    status, code = start(args, CHANNEL)
    if code != "0":
        fail(f"/start answered {status} code {code}")
        return
    took = wait_ready(args, auth)
    if took is None:
        fail(f"{args.uploader} did not answer within 90 s of /start")
        return
    ok(f"a fresh worker answered {took:.0f} s after /start")
    state = state_of(args, a_id, auth) or {}
    record = state.get("record") or {}
    if state.get("state") == "archived" and record.get("topics") is not None:
        ok(f"A still archived, record with {len(record['topics'])} topics")
    else:
        fail(f"A reads {state.get('state')} with record "
             f"{'present' if record else 'missing'} from a fresh worker")
    status, body = request(
        f"{args.uploader}/meeting/{a_id}/record.json", headers=auth
    )
    if status == 200:
        with open(os.path.join(out_dir, "record.json"), "wb") as f:
            f.write(body)
        ok("record.json fetched")
    else:
        fail(f"record.json answered {status}")
    info(f"left running: {CHANNEL}, reaped about {args.timeout} s from now")
    info(f"results in {out_dir}")


def main():
    env = board.dotenv()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--audio",
                   default=os.path.expanduser("~/meeting_probe/M_R003S01C01.wav"),
                   help="a meeting: .wav or .ogg, 16 kHz mono")
    p.add_argument("--minutes", type=float, default=4.0,
                   help="upload only the first N minutes (long enough to "
                   "still be processing when the intruder starts)")
    p.add_argument("--speakers", type=int, default=6)
    p.add_argument(
        "--server",
        default=f"http://127.0.0.1:{setting(env, 'SERVER_PORT', '8081')}",
    )
    p.add_argument("--uploader", default="http://127.0.0.1:8765")
    p.add_argument("--token", help="the uploader's auth_token, if one is set")
    p.add_argument("--timeout", type=int, default=60,
                   help="/start timeout; short, so the reaping is quick")
    p.add_argument("--max-minutes", type=float, default=30.0)
    p.add_argument("--out", default="~/meeting_probe/worker_check")
    args = p.parse_args()

    if checkout():
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
