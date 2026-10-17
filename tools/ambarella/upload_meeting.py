#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Send a meeting's recording to the board from this machine, the way the
Android app does (meeting-app-android board/BoardApi.kt): /start the meeting
worker on the app's channel, meeting-room (a /ping instead when it already
runs), wait for its uploader, then upload the file as multipart with the
app's fields -- saying how much went and how fast. The board then takes it
as any phone's meeting, its screen included. With --follow, follow the
meeting to its end and bring its results back, as the app keeps them:
minutes.txt and record.json, into ambarella_ai_developer_kit_doc/meetings/
<id>/ (git-ignored; --out picks another folder), with minutes.html to read
them in (minutes_html.py). --fetch brings an earlier
meeting's back -- the uploader that serves them runs only with a worker,
so it starts one as the app would.

  python3 tools/ambarella/upload_meeting.py meeting.ogg --speakers 15
  python3 tools/ambarella/upload_meeting.py meeting.ogg --speakers 15 --follow
  python3 tools/ambarella/upload_meeting.py --fetch pc-20261016-181231

For a recording the phone could not send, pull it off the phone first, by
way of its Download folder (a PowerShell > would mangle the bytes):

  adb shell "run-as io.ten.meetingminutes cat files/recordings/<name>.ogg > /sdcard/Download/meeting.ogg"
  adb pull /sdcard/Download/meeting.ogg

Run it here, not on the board. Standard library only. Exits non-zero when
the board refused the meeting, or with --follow or --fetch when it did not
end archived.
"""

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import minutes_html

CHANNEL = "meeting-room"  # the app's: domain/Small.kt Ids.CHANNEL
GRAPH = "meeting_minutes"
FINAL = ("archived", "failed", "empty")
CHUNK = 64 * 1024
RESULTS = ("minutes.txt", "record.json")
DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "ambarella_ai_developer_kit_doc",
    "meetings",
)


def post_json(url, body, timeout=15):
    """(status, the reply's JSON or {}), errors included: the Go server
    answers a channel already running with 400 and code 10003."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as answer:
            status, text = answer.status, answer.read()
    except urllib.error.HTTPError as err:
        status, text = err.code, err.read()
    try:
        return status, json.loads(text or b"{}")
    except ValueError:
        return status, {}


def get_json(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as answer:
            return answer.status, json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, {}
    except (OSError, ValueError):
        return None, {}


def begin(server, worker_timeout_s):
    """The worker on the app's channel: started, or pinged when it runs."""
    status, reply = post_json(
        f"{server}/start",
        {
            "request_id": str(uuid.uuid4()),
            "channel_name": CHANNEL,
            "graph_name": GRAPH,
            "timeout": worker_timeout_s,
        },
    )
    code = str(reply.get("code"))
    if code == "0":
        print(f"  ok    /start {CHANNEL}: the meeting worker is starting")
        return True
    if code == "10003":
        post_json(f"{server}/ping", {"request_id": str(uuid.uuid4()), "channel_name": CHANNEL})
        print(f"  ok    the meeting worker on {CHANNEL} runs already: pinged")
        return True
    print(f"  FAIL  /start answered {status} {reply}")
    return False


def wait_ready(uploader, seconds=60):
    began = time.monotonic()
    while time.monotonic() - began < seconds:
        if get_json(f"{uploader}/meetings", timeout=3)[0] == 200:
            print(f"  ok    the uploader answers, {time.monotonic() - began:.0f} s after")
            return True
        time.sleep(1)
    print(f"  FAIL  the uploader did not answer within {seconds} s")
    return False


def multipart(fields, boundary):
    head = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    )
    head += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        'filename="meeting.ogg"\r\nContent-Type: audio/ogg\r\n\r\n'
    ).encode()
    return head, f"\r\n--{boundary}--\r\n".encode()


def upload(host, port, path, fields, say_every_s=5.0):
    """(status, reply, bytes of the file sent, seconds): streamed, as the
    app sends it, a line every say_every_s with how far it got."""
    boundary = "----meeting" + uuid.uuid4().hex
    head, tail = multipart(fields, boundary)
    size = os.path.getsize(path)
    conn = http.client.HTTPConnection(host, port, timeout=60)
    began = time.monotonic()
    sent = 0
    try:
        conn.putrequest("POST", "/meeting/upload")
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(len(head) + size + len(tail)))
        conn.endheaders()
        conn.send(head)
        said = began
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                conn.send(chunk)
                sent += len(chunk)
                now = time.monotonic()
                if now - said >= say_every_s:
                    print(f"        {sent / 2**20:.1f} of {size / 2**20:.1f} MB, "
                          f"{sent / 1024 / max(now - began, 1e-6):.0f} KB/s", flush=True)
                    said = now
        conn.send(tail)
        answer = conn.getresponse()
        status, text = answer.status, answer.read()
    except OSError as err:
        took = time.monotonic() - began
        print(f"  FAIL  the upload broke after {sent / 2**20:.1f} MB, {took:.0f} s: {err!r}")
        return None, {}, sent, took
    finally:
        conn.close()
    took = time.monotonic() - began
    try:
        reply = json.loads(text or b"{}")
    except ValueError:
        reply = {"error": text[:200].decode(errors="replace")}
    return status, reply, sent, took


def follow(uploader, meeting_id, poll_s, give_up_s=1800.0):
    """The meeting's states as they change, to its end; None when the
    uploader stopped answering for give_up_s -- the worker died, or the
    board went away -- said once when it stops and once if it comes back."""
    last = None
    silent_since = None
    while True:
        status, reply = get_json(f"{uploader}/meeting/{meeting_id}")
        if status is None:
            now_s = time.monotonic()
            if silent_since is None:
                silent_since = now_s
                print(f"  warn  {time.strftime('%H:%M:%S')}  the board stopped answering: "
                      "its meeting worker may have stopped, or the network dropped", flush=True)
            elif now_s - silent_since > give_up_s:
                print(f"  FAIL  no answer for {give_up_s / 60:.0f} minutes; when the board is back: "
                      f"python3 tools/ambarella/upload_meeting.py --fetch {meeting_id}", flush=True)
                return None
            time.sleep(poll_s)
            continue
        if silent_since is not None:
            print(f"  ok    {time.strftime('%H:%M:%S')}  answering again", flush=True)
            silent_since = None
        if status == 200:
            progress = reply.get("progress") or {}
            now = (reply.get("state"), progress.get("topics_done"), progress.get("topics_total"))
            if now != last:
                print(f"  {time.strftime('%H:%M:%S')}  {now[0]:<13} {now[1]}/{now[2]}", flush=True)
                last = now
            if now[0] in FINAL:
                if reply.get("error"):
                    print(f"        {reply['error']}")
                return now[0]
        time.sleep(poll_s)


def fetch_results(uploader, meeting_id, out):
    """minutes.txt and record.json into out/<id>/; the paths written."""
    folder = os.path.join(out, meeting_id)
    written = []
    for name in RESULTS:
        try:
            with urllib.request.urlopen(f"{uploader}/meeting/{meeting_id}/{name}", timeout=30) as answer:
                data = answer.read()
        except (OSError, ValueError) as err:
            print(f"  warn  no {name}: {err}")
            continue
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(data)
        written.append(path)
        print(f"  ok    {name} -> {path}")
    record = os.path.join(folder, "record.json")
    if record in written:
        try:
            page = minutes_html.write(record)
        except (OSError, ValueError) as err:
            print(f"  warn  no minutes.html: {err}")
        else:
            written.append(page)
            print(f"  ok    minutes.html -> {page}  (open it in a browser)")
    return written


def fetch(uploader, meeting_id, out):
    """An earlier meeting's results, when it has ended; 0 if archived."""
    status, reply = get_json(f"{uploader}/meeting/{meeting_id}")
    if status != 200:
        print(f"  FAIL  the board has no meeting {meeting_id} ({status})")
        return 1
    state = reply.get("state")
    if state not in FINAL:
        print(f"  FAIL  {meeting_id} is still {state}: nothing to bring back yet")
        return 1
    print(f"  ok    {meeting_id} ended {state}" + (f": {reply['error']}" if reply.get("error") else ""))
    fetch_results(uploader, meeting_id, out)
    return 0 if state == "archived" else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("audio", nargs="?", help="the recording, 16 kHz mono Ogg-Opus as the app records it")
    p.add_argument("--fetch", metavar="ID", help="bring an earlier meeting's results back instead")
    p.add_argument("--out", default=DEFAULT_OUT, help="where results land, a folder per meeting")
    p.add_argument("--host", default="192.168.0.19", help="the board (192.168.0.19)")
    p.add_argument("--server-port", type=int, default=8081, help="its API server (8081)")
    p.add_argument("--uploader-port", type=int, default=8765, help="its meeting uploader (8765)")
    p.add_argument("--speakers", type=int, help="how many attend, as entered in the app: an upper bound, the board finds how many spoke")
    p.add_argument("--title", default="", help="the meeting's title")
    p.add_argument("--script", default="traditional", help="traditional (the app's default) or simplified")
    p.add_argument("--id", help="the meeting's id (pc-<date>-<time> by default)")
    p.add_argument("--timeout", type=int, default=600, help="the worker's idle timeout, as the app's (600 s)")
    p.add_argument("--follow", action="store_true", help="follow the meeting to its end")
    p.add_argument("--give-up-min", type=float, default=30.0,
                   help="with --follow, stop after the board has not answered this long (30)")
    p.add_argument("--poll-s", type=float, default=10.0, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if bool(args.audio) == bool(args.fetch):
        p.error("give a recording to send, or --fetch ID")

    server = f"http://{args.host}:{args.server_port}"
    uploader = f"http://{args.host}:{args.uploader_port}"
    if args.fetch:
        print(f"{args.fetch}: its results from {args.host}")
        if not begin(server, args.timeout) or not wait_ready(uploader):
            return 1
        return fetch(uploader, args.fetch, args.out)
    meeting_id = args.id or time.strftime("pc-%Y%m%d-%H%M%S")
    size = os.path.getsize(args.audio)
    print(f"{args.audio}: {size / 2**20:.1f} MB, to {args.host} as {meeting_id}")
    if not begin(server, args.timeout) or not wait_ready(uploader):
        return 1
    fields = {"meeting_id": meeting_id, "script": args.script}
    if args.speakers:
        fields["speakers"] = str(args.speakers)
    if args.title:
        fields["title"] = args.title
    status, reply, sent, took = upload(args.host, args.uploader_port, args.audio, fields)
    if status is None:
        return 1
    print(f"        sent {sent / 2**20:.1f} MB in {took:.1f} s, {sent / 1024 / max(took, 1e-6):.0f} KB/s")
    if status != 200:
        print(f"  FAIL  the board answered {status}: {reply.get('error') or reply}")
        return 1
    print(f"  ok    {reply.get('status')}: {reply.get('meeting_id')}, "
          f"{reply.get('ahead', 0)} ahead, about {reply.get('wait_s', 0):.0f} s to wait")
    if not args.follow:
        print(f"        its results, once it ends: python3 tools/ambarella/upload_meeting.py --fetch {meeting_id}")
        return 0
    final = follow(uploader, meeting_id, args.poll_s, args.give_up_min * 60)
    if final is None:
        return 1
    fetch_results(uploader, meeting_id, args.out)
    return 0 if final == "archived" else 1


if __name__ == "__main__":
    sys.exit(main())
