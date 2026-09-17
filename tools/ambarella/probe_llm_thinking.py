#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Measure how long the board's model spends thinking, and whether asking it
not to changes that.

The model is Deepseek-R1-Distill-Qwen, which writes its reasoning before it
answers. Measured on 2026-09-16, the first answer character arrived 26.2 s,
45.4 s and 43.0 s after the question, for answers of 67, 8 and 496 characters.
The time is not in the answer.

The board's HTTP interface has no setting for this -- the guide documents four
headers and none of them touches generation. The one untried lever is the
prompt, so this asks the same questions several ways and reports, for each:

  think     seconds until </think>, which is the thinking
  answer    seconds from there to the last character
  chars     how much reasoning and how much answer

A variant only helps if `think` falls without `answer` getting worse or the
reply getting shorter. All four numbers are printed so that is visible rather
than asserted.

  python3 tools/ambarella/probe_llm_thinking.py
  python3 tools/ambarella/probe_llm_thinking.py --settle 30
  python3 tools/ambarella/probe_llm_thinking.py --repeat 2

Sends requests to the LLM and reads the replies. Starts nothing, stops nothing,
changes no configuration. The board serves one user at a time, so it pauses
between requests -- without that, a probe fails merely because the one before
it is still generating.
"""

import argparse
import re
import socket
import sys
import time
import urllib.parse

# From the developer kit guide's own curl example. Session-Id must be a
# non-zero decimal integer: the server parses it numerically and a
# non-numeric value is refused with no HTTP response at all.
CLOSE_TAG = "</think>"
ESCAPES = {"<SP>": " ", "<NL>": "\n"}
DONE_TOKENS = ("<DONE>", "[DONE]")

QUESTIONS = [
    "你是谁？",
    "讲一个笑话。",
    "六岁小孩怎么学加法？",
]

# Each variant changes exactly one thing about the prompt. The first is the
# control: it is what the graph sends today.
VARIANTS = {
    "as deployed": "{q}",
    "/no_think": "/no_think {q}",
    "no_think tag": "{q} /no_think",
    "told in chinese": "直接回答，不要思考过程。{q}",
    "prefilled block": "{q}\n<think>\n</think>",
}


def _payload_of(raw: bytes) -> bytes:
    """Everything after the header block, or all of it if none has arrived."""
    split = raw.find(b"\r\n\r\n")
    return raw[split + 4 :] if split >= 0 else raw


def decode(raw: bytes) -> str:
    """Undo the board's SSE framing and whitespace escapes."""
    text = raw.decode("utf-8", "replace")
    out = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:]
        if payload.startswith(" "):
            payload = payload[1:]
        if payload.strip() in DONE_TOKENS:
            break
        out.append(payload)
    joined = "".join(out)
    for token, char in ESCAPES.items():
        joined = joined.replace(token, char)
    return joined


def ask(host, port, session_id, model_type, query, timeout):
    """One request, timing the closing tag as it goes past.

    Read over a raw socket rather than with a client library: the board's
    framing has already been seen to disagree with its own Content-Type, and
    a library that refuses to parse it hands back nothing to measure.
    """
    body = query.encode("utf-8")
    request = (
        b"POST / HTTP/1.1\r\n"
        + f"Host: {host}:{port}\r\n".encode()
        + f"Session-Id: {session_id}\r\n".encode()
        + f"Model-Type: {model_type}\r\n".encode()
        + b"Stream-Off: 0\r\nReset-En: 1\r\n"
        + b"Content-Type: text/plain; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )

    started = time.monotonic()
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
    except OSError as err:
        return {"error": f"connect failed: {err}"}

    received = bytearray()
    first_byte_at = None
    close_tag_at = None
    conn.settimeout(timeout)
    try:
        conn.sendall(request)
        while True:
            chunk = conn.recv(8192)
            if not chunk:
                break
            if first_byte_at is None:
                first_byte_at = time.monotonic()
            received.extend(chunk)
            # The board sends one character per SSE event, so the tag never
            # appears contiguously in the raw stream -- looking for it there
            # finds nothing however long it thinks. Decode first.
            if close_tag_at is None and CLOSE_TAG in decode(
                _payload_of(bytes(received))
            ):
                close_tag_at = time.monotonic()
    except socket.timeout:
        return {"error": f"no end of response within {timeout:.0f}s"}
    except OSError as err:
        return {"error": f"read failed: {err}"}
    finally:
        conn.close()
    finished = time.monotonic()

    raw = bytes(received)
    if not raw:
        return {"error": "the server closed without sending anything"}

    split = raw.find(b"\r\n\r\n")
    head = raw[:split].decode("utf-8", "replace") if split >= 0 else ""
    text = decode(_payload_of(raw))
    if CLOSE_TAG in text:
        reasoning, _, answer = text.partition(CLOSE_TAG)
    else:
        reasoning, answer = "", text

    return {
        "status": head.split("\r\n")[0] if head else "<no status line>",
        "think_s": (close_tag_at - started) if close_tag_at else None,
        "first_byte_s": (first_byte_at - started) if first_byte_at else None,
        "total_s": finished - started,
        "reasoning_chars": len(reasoning.strip()),
        "answer_chars": len(answer.strip()),
        "answer": answer.strip(),
        "bytes": len(raw),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--model-type", default="9")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--settle",
        type=float,
        default=15.0,
        help="pause between requests; the board serves one user at a time",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--session-id",
        default="815217",
        help="reused for every request, the way the extension does",
    )
    parser.add_argument(
        "--only",
        action="append",
        help="run just this variant, repeatable",
    )
    args = parser.parse_args()

    url = urllib.parse.urlparse(args.url)
    host, port = url.hostname, url.port or 8080

    variants = {
        name: form
        for name, form in VARIANTS.items()
        if not args.only or name in args.only
    }
    if not variants:
        sys.exit(f"no such variant. known: {', '.join(VARIANTS)}")

    print(f"target      {args.url}  model_type={args.model_type}")
    print(f"session     {args.session_id}, reused (the board serves one user)")
    print(f"settle      {args.settle:.0f}s between requests")
    print(f"questions   {len(QUESTIONS)}  variants {len(variants)}  "
          f"repeat {args.repeat}")
    print(f"total       {len(QUESTIONS) * len(variants) * args.repeat} requests, "
          f"allow roughly "
          f"{len(QUESTIONS) * len(variants) * args.repeat * (args.settle + 45) / 60:.0f}"
          f" minutes")

    # One session id for the whole run, not one per request. The board keeps a
    # session alive for 180 s after a reply and serves --max_user 1, so a fresh
    # id per request means the second one is refused with
    # "current user num (2) > max_user_num (1)" and the probe measures nothing.
    # The extension reuses one id per conversation; this matches it. Reset-En is
    # already 1 on every request, which is what keeps the turns independent.
    session = args.session_id
    rows = []
    for round_no in range(args.repeat):
        for question in QUESTIONS:
            print(f"\n\033[1m{question}\033[0m")
            print(
                f"  {'variant':<18}{'think':>8}{'answer':>9}"
                f"{'reason':>8}{'reply':>7}  first words"
            )
            for name, form in variants.items():
                time.sleep(args.settle)
                query = form.format(q=question)
                result = ask(
                    host, port, session, args.model_type, query, args.timeout
                )
                if "error" in result:
                    print(f"  {name:<18}  \033[31m{result['error']}\033[0m")
                    rows.append((question, name, None, None, 0, 0))
                    continue

                think = result["think_s"]
                answer_s = (
                    result["total_s"] - think if think else result["total_s"]
                )
                # No tag means it never entered a thinking block: the whole
                # reply is answer, and the thinking cost is zero.
                think_txt = f"{think:.1f}s" if think else "   0.0s"
                print(
                    f"  {name:<18}{think_txt:>8}{answer_s:>8.1f}s"
                    f"{result['reasoning_chars']:>8}"
                    f"{result['answer_chars']:>7}  "
                    f"{result['answer'][:24]!r}"
                )
                rows.append(
                    (
                        question,
                        name,
                        think if think is not None else 0.0,
                        answer_s,
                        result["reasoning_chars"],
                        result["answer_chars"],
                    )
                )

    print("\n\033[1m===== Summary\033[0m")
    print(f"  {'variant':<18}{'think avg':>11}{'reason avg':>12}"
          f"{'reply avg':>11}{'failed':>8}")
    for name in variants:
        mine = [r for r in rows if r[1] == name]
        ok = [r for r in mine if r[2] is not None]
        failed = len(mine) - len(ok)
        if not ok:
            print(f"  {name:<18}{'--':>11}{'--':>12}{'--':>11}{failed:>8}")
            continue
        think = sum(r[2] for r in ok) / len(ok)
        reason = sum(r[4] for r in ok) / len(ok)
        reply = sum(r[5] for r in ok) / len(ok)
        print(
            f"  {name:<18}{think:>10.1f}s{reason:>12.0f}"
            f"{reply:>11.0f}{failed:>8}"
        )

    print()
    print("  A variant is worth using only if 'think avg' falls and")
    print("  'reply avg' does not. A short reply with a short think is the")
    print("  model refusing to answer, not thinking faster -- the first")
    print("  words printed above are there to tell those apart.")
    print()
    print("  A think of 0.0s means the reply carried no </think> at all,")
    print("  which is the variant working completely.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
