#!/usr/bin/env python3
"""Does the board split a UTF-8 character across two SSE events?

Sends one Chinese question over a raw socket and looks at the bytes, not at
the decoded text: for every `data:` line, whether its payload is valid UTF-8
on its own. A payload that is only part of a character means the newline
separating the events landed inside it, which is what turns into U+FFFD.

  python3 sse_bytes.py
  python3 sse_bytes.py --query "讲一个笑话。"
"""
import argparse, socket, sys

p = argparse.ArgumentParser()
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--port", type=int, default=8080)
p.add_argument("--session-id", default="815217")
p.add_argument("--model-type", default="9")
p.add_argument("--query", default="用中文说一句话，要包含「想」和「样」。")
p.add_argument("--timeout", type=float, default=180.0)
a = p.parse_args()

body = a.query.encode("utf-8")
req = (
    b"POST / HTTP/1.1\r\n"
    + f"Host: {a.host}:{a.port}\r\n".encode()
    + f"Session-Id: {a.session_id}\r\n".encode()
    + f"Model-Type: {a.model_type}\r\n".encode()
    + b"Stream-Off: 0\r\nReset-En: 1\r\n"
    + b"Content-Type: text/plain; charset=utf-8\r\n"
    + f"Content-Length: {len(body)}\r\n".encode()
    + b"Connection: close\r\n\r\n"
    + body
)

conn = socket.create_connection((a.host, a.port), timeout=a.timeout)
raw = bytearray()
try:
    conn.sendall(req)
    while True:
        chunk = conn.recv(8192)
        if not chunk:
            break
        raw.extend(chunk)
finally:
    conn.close()

raw = bytes(raw)
split = raw.find(b"\r\n\r\n")
payload_area = raw[split + 4:] if split >= 0 else raw
print(f"received {len(raw)} bytes")

lines = payload_area.split(b"\n")
events = [l for l in lines if l.startswith(b"data:")]
print(f"{len(events)} data: events")

bad = []
for i, line in enumerate(events):
    pay = line[5:]
    if pay.startswith(b" "):
        pay = pay[1:]
    pay = pay.rstrip(b"\r")
    try:
        pay.decode("utf-8")
    except UnicodeDecodeError:
        bad.append((i, pay))

if not bad:
    print("\nevery event's payload is valid UTF-8 on its own")
    print("so the board is not splitting characters across events")
else:
    print(f"\n{len(bad)} of {len(events)} events carry a partial character:")
    for i, pay in bad[:12]:
        ctx = b"".join(e[5:].lstrip(b" ").rstrip(b"\r") for e in events[max(0, i-1):i+3])
        print(f"  event {i}: {pay.hex(' ')}"
              f"   with its neighbours: {ctx.hex(' ')}"
              f"  -> {ctx.decode('utf-8', 'replace')!r}")
    print("\nthe newline separating events lands inside a character,")
    print("which is what becomes U+FFFD once the stream is decoded")
