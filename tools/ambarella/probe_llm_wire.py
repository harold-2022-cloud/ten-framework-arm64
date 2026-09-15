#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Look at what the board's LLM actually puts on the wire.

curl reports `(8) Header without colon` and stops. That says the response is
not well-formed HTTP but not which byte broke it, and curl will not hand over
a response it refuses to parse. This speaks HTTP over a raw socket instead and
prints the bytes, so the diagnosis comes from the response rather than from
curl's opinion of it.

It also finds test_llm's own log by asking the running process which files it
has open, rather than assuming /tmp/log.txt.

  python3 tools/ambarella/probe_llm_wire.py
  python3 tools/ambarella/probe_llm_wire.py --host 127.0.0.1 --port 8080
  python3 tools/ambarella/probe_llm_wire.py --settle 30    # after a busy turn

Reads only. It sends requests to the LLM and reads files; it changes nothing.
Exit code is 0 if any probe got a well-formed HTTP response, 1 otherwise.
"""

import argparse
import os
import socket
import sys
import time

BAR = "=" * 78


def say(title):
    print(f"\n{BAR}\n{title}\n{BAR}")


def kv(key, value):
    print(f"  {key:<34} {value}")


# ----------------------------------------------------------------- processes


def read_proc(pid, name):
    try:
        with open(f"/proc/{pid}/{name}", "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def find_llm_processes():
    """Every process whose argv mentions test_llm, with cwd and open files."""
    found = []
    for entry in sorted(os.listdir("/proc")):
        if not entry.isdigit():
            continue
        cmdline = read_proc(entry, "cmdline").replace(b"\0", b" ").decode(
            "utf-8", "replace"
        ).strip()
        if "test_llm" not in cmdline:
            continue
        try:
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except OSError:
            cwd = "<unreadable>"
        files = []
        fd_dir = f"/proc/{entry}/fd"
        try:
            for fd in sorted(os.listdir(fd_dir), key=lambda x: int(x)):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                # Sockets, pipes and devices are not the log.
                if target.startswith("/") and not target.startswith(
                    ("/dev/", "/proc/", "/sys/")
                ):
                    files.append(target)
        except OSError:
            pass
        found.append((entry, cmdline, cwd, files))
    return found


def report_processes():
    say("1. The LLM's own processes and log")
    processes = find_llm_processes()
    if not processes:
        kv("test_llm processes", "**NONE** -- nothing is serving")
        return []

    logs = []
    for pid, cmdline, cwd, files in processes:
        print(f"\n  pid {pid}")
        print(f"    argv  {cmdline}")
        print(f"    cwd   {cwd}")
        if files:
            for path in files:
                print(f"    open  {path}")
                if path.endswith((".txt", ".log")):
                    logs.append(path)
        else:
            print("    open  (no regular files)")

    # The log is written by test_llm itself. Its path is whatever the process
    # opened, which depends on the directory it was started from -- assuming
    # /tmp/log.txt is how the earlier diagnostic came to report a model as
    # missing when it had simply looked in the wrong place.
    print()
    if logs:
        for path in dict.fromkeys(logs):
            kv("log found", path)
    else:
        kv("log found", "none open; test_llm may log to stdout only")
        for _, _, cwd, _ in processes:
            guess = os.path.join(cwd, "log.txt")
            kv("  not open, checked", f"{guess} ({os.path.exists(guess)})")
    return list(dict.fromkeys(logs))


def tail(path, lines):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.readlines()
    except OSError as err:
        return [f"<cannot read: {err}>"]
    return [line.rstrip("\n") for line in content[-lines:]]


# --------------------------------------------------------------------- probes


def send_raw(host, port, request, timeout, read_bytes):
    """One request, one raw read. Returns (bytes, error)."""
    try:
        connection = socket.create_connection((host, port), timeout=timeout)
    except OSError as err:
        return b"", f"connect failed: {err}"
    received = bytearray()
    try:
        connection.sendall(request)
        connection.settimeout(timeout)
        while len(received) < read_bytes:
            chunk = connection.recv(8192)
            if not chunk:
                break
            received.extend(chunk)
    except socket.timeout:
        # Not an error: a streaming reply simply has not ended yet, and what
        # arrived is enough to judge the framing.
        pass
    except OSError as err:
        return bytes(received), f"read failed: {err}"
    finally:
        connection.close()
    return bytes(received), ""


def analyse(raw):
    """Say precisely where HTTP parsing succeeds or breaks.

    This is the part curl will not show: which line it objected to.
    """
    if not raw:
        print("    nothing received -- the server closed without a byte")
        return False

    print(f"    bytes received  {len(raw)}")
    print(f"    first 160 bytes {raw[:160]!r}")

    split = raw.find(b"\r\n\r\n")
    separator = 4
    if split < 0:
        split = raw.find(b"\n\n")
        separator = 2
    if split < 0:
        print("    no blank line: the response has no header block at all")
        print("    -> this is a bare body, which curl rejects as HTTP/0.9")
        return False

    head = raw[:split].decode("utf-8", "replace")
    body = raw[split + separator :]
    lines = head.split("\n")
    status = lines[0].rstrip("\r")

    print(f"    status line     {status!r}")
    if not status.startswith("HTTP/"):
        print("    -> not a status line; everything before the blank line is")
        print("       being read as headers, and the first line has no colon.")
        print("       THIS IS WHAT curl REPORTS AS (8) Header without colon.")
        return False

    well_formed = True
    for line in lines[1:]:
        line = line.rstrip("\r")
        if not line:
            continue
        if ":" in line:
            print(f"    header          {line}")
        else:
            well_formed = False
            print(f"    header          {line!r}   <-- NO COLON, breaks parsing")

    preview = body[:300].decode("utf-8", "replace")
    print(f"    body bytes      {len(body)}")
    print(f"    body preview    {preview!r}")
    return well_formed


def build_request(host, port, method, path, headers, body):
    payload = body.encode("utf-8") if isinstance(body, str) else body
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}"]
    lines += [f"{key}: {value}" for key, value in headers]
    if payload is not None:
        lines.append(f"Content-Length: {len(payload)}")
    lines.append("Connection: close")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
    return head + (payload or b"")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--session-id", default="1234")
    parser.add_argument("--model-type", default="9")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--settle",
        type=float,
        default=8.0,
        help="pause between probes; the board serves one user at a time",
    )
    parser.add_argument("--read-bytes", type=int, default=4096)
    parser.add_argument("--log-lines", type=int, default=25)
    args = parser.parse_args()

    say("0. Context")
    kv("target", f"http://{args.host}:{args.port}/")
    kv("session id", args.session_id)
    kv("model type", args.model_type)
    kv("settle between probes", f"{args.settle}s")

    logs = report_processes()

    common = [
        ("Session-Id", args.session_id),
        ("Model-Type", args.model_type),
        ("Reset-En", "1"),
    ]

    # Each probe changes exactly one thing from the one above it, so whichever
    # first fails names the cause. P0 is the control: curl already gets a
    # well-formed 404 from it, so a failure there would mean the probe itself
    # is wrong rather than the server.
    probes = [
        ("P0", "GET /health -- control, known to answer 404", "GET", "/health", [], None),
        ("P1", "POST, ASCII body, non-streaming", "POST", "/", common + [("Stream-Off", "1")], "Hello"),
        ("P2", "+ multibyte body", "POST", "/", common + [("Stream-Off", "1")], "你好"),
        ("P3", "+ Content-Type the extension sends", "POST", "/", common + [("Stream-Off", "1"), ("Content-Type", "text/plain; charset=utf-8")], "你好"),
        ("P4", "streaming -- the extension's real request", "POST", "/", common + [("Stream-Off", "0"), ("Content-Type", "text/plain; charset=utf-8")], "你好，一句話介紹自己"),
        ("P5", "control: P1 again", "POST", "/", common + [("Stream-Off", "1")], "Hello"),
    ]

    say("2. Wire probes")
    results = {}
    for tag, description, method, path, headers, body in probes:
        print(f"\n  --- {tag}: {description}")
        if tag != "P0":
            time.sleep(args.settle)

        before = {}
        for log in logs:
            try:
                before[log] = os.path.getsize(log)
            except OSError:
                before[log] = 0

        request = build_request(args.host, args.port, method, path, headers, body)
        print(f"    request         {request[:120]!r}")
        raw, error = send_raw(
            args.host, args.port, request, args.timeout, args.read_bytes
        )
        if error:
            print(f"    transport       {error}")
        results[tag] = analyse(raw)

        for log in logs:
            try:
                size = os.path.getsize(log)
            except OSError:
                continue
            if size > before.get(log, 0):
                with open(log, "rb") as handle:
                    handle.seek(before.get(log, 0))
                    added = handle.read().decode("utf-8", "replace")
                print(f"    {log} added:")
                for line in added.splitlines():
                    print(f"      {line}")

    if logs:
        say("3. Tail of the LLM's log")
        for log in logs:
            print(f"\n  {log}")
            for line in tail(log, args.log_lines):
                print(f"    {line}")

    say("4. Verdict")
    for tag, _, _, _, _, _ in probes:
        state = "well-formed HTTP" if results.get(tag) else "**MALFORMED**"
        kv(tag, state)

    print()
    if results.get("P0") and not results.get("P1"):
        print("  P0 answers HTTP and P1 does not: the server speaks HTTP for")
        print("  the routes it knows and writes the generation straight to the")
        print("  socket. Read P1's first 160 bytes above -- they are the reply,")
        print("  and no client that insists on HTTP will accept them.")
    elif not results.get("P0"):
        print("  Even the control is malformed. Check the port: this may not")
        print("  be the LLM's user-facing port.")
    elif all(results.get(t) for t in ("P1", "P5")):
        print("  The server answers correctly. Whatever curl objected to is")
        print("  not reproducible here; compare the request lines above.")
    else:
        print("  Read the ladder above: the first FAILED row names the cause.")

    return 0 if any(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
