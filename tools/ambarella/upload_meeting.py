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
meeting to its end.

  python3 tools/ambarella/upload_meeting.py meeting.ogg --speakers 15
  python3 tools/ambarella/upload_meeting.py meeting.ogg --speakers 15 --follow

For a recording the phone could not send, pull it off the phone first, by
way of its Download folder (a PowerShell > would mangle the bytes):

  adb shell "run-as io.ten.meetingminutes cat files/recordings/<name>.ogg > /sdcard/Download/meeting.ogg"
  adb pull /sdcard/Download/meeting.ogg

Run it here, not on the board. Standard library only. Exits non-zero when
the board refused the meeting, or with --follow when it did not end archived.
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

CHANNEL = "meeting-room"  # the app's: domain/Small.kt Ids.CHANNEL
GRAPH = "meeting_minutes"
FINAL = ("archived", "failed", "empty")
CHUNK = 64 * 1024


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


def follow(uploader, meeting_id, poll_s):
    last = None
    while True:
        status, reply = get_json(f"{uploader}/meeting/{meeting_id}")
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


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("audio", help="the recording, 16 kHz mono Ogg-Opus as the app records it")
    p.add_argument("--host", default="192.168.0.19", help="the board (192.168.0.19)")
    p.add_argument("--server-port", type=int, default=8081, help="its API server (8081)")
    p.add_argument("--uploader-port", type=int, default=8765, help="its meeting uploader (8765)")
    p.add_argument("--speakers", type=int, help="how many people spoke, as entered in the app")
    p.add_argument("--title", default="", help="the meeting's title")
    p.add_argument("--script", default="traditional", help="traditional (the app's default) or simplified")
    p.add_argument("--id", help="the meeting's id (pc-<date>-<time> by default)")
    p.add_argument("--timeout", type=int, default=600, help="the worker's idle timeout, as the app's (600 s)")
    p.add_argument("--follow", action="store_true", help="follow the meeting to its end")
    p.add_argument("--poll-s", type=float, default=10.0, help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    server = f"http://{args.host}:{args.server_port}"
    uploader = f"http://{args.host}:{args.uploader_port}"
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
        print(f"        its state: GET {uploader}/meeting/{meeting_id} (or run this with --follow)")
        return 0
    return 0 if follow(uploader, meeting_id, args.poll_s) == "archived" else 1


if __name__ == "__main__":
    sys.exit(main())
