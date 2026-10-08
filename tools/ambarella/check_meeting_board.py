#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Run one meeting through the board's meeting_minutes graph, as a client
would, and say whether each part held.

  python3.12 tools/ambarella/check_meeting_board.py --check
  python3.12 tools/ambarella/check_meeting_board.py \\
      --audio ~/meeting_probe/M_R003S01C01.wav \\
      --rttm ~/meeting_probe/M_R003S01C01.rttm --speakers 6 --minutes 6

Run it on the board, under the interpreter the TEN runtime loads (3.12 on the
N1-655, not the shell's python3): the preflight imports what the meeting
extensions import, and an import that works under another interpreter says
nothing about the graph.

What it checks, in order:

  preflight  the server lists meeting_minutes; the LLM daemon answers; the
             four model files exist; this interpreter has the extensions'
             packages; where meetings land is not tmpfs and has room; nothing
             already holds 8765
  start      POST /start with timeout 60 -- short on purpose: processing
             takes minutes, so a worker still answering after 60 s proves
             the uploader is keeping it alive
  upload     waits for GET /meetings, then uploads the audio (a .wav is
             converted to 16 kHz mono Ogg-Opus first; --minutes cuts it)
  process    polls the state every 10 s, printing each change and the
             worker's resident memory
  result     fetches record.json and minutes.txt into --out, reports topics,
             speakers, summaries, time and memory, and -- with --rttm -- the
             share of speech given to the wrong person
  stop       POST /stop

Exits non-zero if any check failed.
"""

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ENV_FILE = os.path.join(REPO, "ai_agents", ".env")

GRAPH = "meeting_minutes"
FINAL = ("archived", "empty", "failed")
# The graph's own defaults (examples/voice-assistant/tenapp/property.json),
# overridden by the same variables in ai_agents/.env.
MODELS = {
    "DIARIZATION_SEG_MODEL": "/home/lychee/diarization_models/"
    "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
    "DIARIZATION_EMB_MODEL": "/home/lychee/diarization_models/"
    "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
    "SENSEVOICE_MODEL_DIR": "/home/lychee/sensevoice",
    "MEETING_VAD_MODEL": "/home/lychee/vad_models/ten-vad.onnx",
}
PACKAGES = ("numpy", "soundfile", "sherpa_onnx", "aiohttp", "pydantic", "opencc")
# The record's script, as meeting_control_python/script.py writes it: the
# OpenCC conversion, the name to say, and the values it leaves as they are.
SCRIPTS = {"traditional": ("s2tw", "Traditional"), "simplified": ("t2s", "Simplified")}
IDENTIFIERS = {"meeting_id", "id", "audio", "error", "actions_error"}

failures = []


def record_texts(value):
    """Every text the controller converts in record.json."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in IDENTIFIERS:
                yield from record_texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from record_texts(item)
    elif isinstance(value, str) and value.strip():
        yield value


def off_script(record, script):
    """(texts checked, [(text, converted)] for those not in the script). A
    text already in the script is left as it is by the conversion to it --
    on 278 texts from real records, converting a converted one again never
    changed it."""
    from opencc import OpenCC  # pylint: disable=import-outside-toplevel

    convert = OpenCC(SCRIPTS[script][0]).convert
    texts = list(record_texts(record))
    pairs = ((t, convert(t)) for t in texts)
    return len(texts), [(t, c) for t, c in pairs if c != t]


def check_script(record, script):
    name = SCRIPTS[script][1]
    checked, off = off_script(record, script)
    if not checked:
        warn(f"the record has no text to check for {name}")
    elif not off:
        ok(f"the record is all {name} ({checked} texts)")
    else:
        fail(f"{len(off)} of {checked} texts in the record are not {name}",
             "the controller converts as it writes: did the upload's script "
             "(or MEETING_OUTPUT_SCRIPT) say what was meant?")
        for text, converted in off[:3]:
            one, other = (" ".join(x.split())[:50] for x in (text, converted))
            info(f"{one}  ->  {other}")


def say(msg):
    print(f"\n== {msg}", flush=True)


def ok(msg):
    print(f"  ok    {msg}", flush=True)


def info(msg):
    print(f"        {msg}", flush=True)


def fail(msg, fix=None):
    failures.append(msg)
    print(f"  FAIL  {msg}", flush=True)
    if fix:
        print(f"        -> {fix}", flush=True)


def warn(msg):
    print(f"  warn  {msg}", flush=True)


def mmss(seconds):
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def dotenv():
    out = {}
    if os.path.isfile(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def setting(env, key, default):
    return os.environ.get(key) or env.get(key) or default


# --------------------------------------------------------------------------
# HTTP, standard library only


GONE = 0  # nothing listens: connection refused, host unreachable
SLOW = -1  # something listens but did not answer in time


def request(url, body=None, headers=None, timeout=30):
    """(status, bytes); GONE or SLOW when there was no HTTP answer."""
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (socket.timeout, TimeoutError):
        return SLOW, b""
    except urllib.error.URLError as e:
        slow = isinstance(e.reason, (socket.timeout, TimeoutError))
        return (SLOW if slow else GONE), b""
    except OSError:
        return GONE, b""


def post_json(url, payload):
    status, body = request(
        url,
        json.dumps(payload).encode(),
        {"Content-Type": "application/json"},
    )
    try:
        return status, json.loads(body or b"null")
    except ValueError:
        return status, None


def multipart(fields, path):
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
            f"\r\n\r\n{value}\r\n".encode()
        )
    with open(path, "rb") as f:
        audio = f.read()
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="meeting.ogg"\r\nContent-Type: audio/ogg\r\n\r\n'.encode()
        + audio
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------
# preflight


def preflight(args, env):
    say("preflight")
    status, reply = request(f"{args.server}/graphs", timeout=5)
    if status <= 0:
        fail(
            f"no Go server at {args.server}",
            "start it: cd ai_agents/agents/examples/voice-assistant && task run",
        )
    else:
        names = json.dumps(json.loads(reply or b"{}").get("data"))
        if f'"{GRAPH}"' in names:
            ok(f"{args.server}/graphs lists {GRAPH}")
        else:
            fail(
                f"{args.server}/graphs has no {GRAPH}",
                "a graph added after the server started is not picked up: "
                "restart the server and the playground",
            )

    llm = setting(env, "AMBARELLA_LLM_BASE_URL", "http://127.0.0.1:8080")
    host_port = llm.split("//", 1)[-1].split("/", 1)[0]
    host, _, port = host_port.partition(":")
    try:
        socket.create_connection((host, int(port or 80)), timeout=3).close()
        ok(f"something listens at {llm} (the LLM daemon)")
    except OSError:
        fail(
            f"nothing listens at {llm}",
            "start the board's LLM daemon; tools/ambarella/check_llm_board.sh",
        )

    for key, default in MODELS.items():
        path = setting(env, key, default)
        if os.path.exists(path):
            ok(f"{key} {path}")
        else:
            fail(
                f"{key} {path} is missing",
                "tools/ambarella/install_meeting_models.sh, and put the paths "
                "it prints in ai_agents/.env if they differ",
            )

    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if version != "3.12":
        warn(
            f"running under Python {version}; the runtime on the N1-655 "
            "loads 3.12, so the imports below may not speak for it"
        )
    for name in PACKAGES:
        try:
            __import__(name)
            ok(f"import {name}")
        except ImportError as err:
            fail(
                f"import {name}: {err}",
                "re-run ai_agents/agents/scripts/install_board_arm64.sh "
                "(no arguments; voice-assistant is its default): "
                "it installs every extension's "
                "requirements.txt for the runtime's interpreter",
            )

    where = setting(env, "MEETINGS_DIR", "/tmp/meetings")
    fs = filesystem_of(where)
    if fs == "tmpfs":
        warn(
            f"{where} is on tmpfs: archives live in RAM and are gone after a "
            "reboot. Set MEETINGS_DIR in ai_agents/.env to a disk path"
        )
    else:
        ok(f"{where} is on {fs}")
    free = free_mb(where)
    if free < 250 + 64:
        fail(f"{free:.0f} MB free under {where}; a meeting needs ~300")
    else:
        ok(f"{free:.0f} MB free under {where}")

    status, _ = request(f"{args.uploader}/meetings", timeout=3)
    if status != GONE:
        warn(
            f"{args.uploader} already answers: a meeting worker is running. "
            "This check starts its own; stop the other one first"
        )
    else:
        ok(f"{args.uploader} is free")


def filesystem_of(path):
    path = os.path.realpath(path if os.path.exists(path) else os.path.dirname(path))
    best, fs = "", "?"
    for line in open("/proc/mounts", encoding="utf-8"):
        _, mount, kind = line.split()[:3]
        inside = path == mount or path.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) > len(best):
            best, fs = mount, kind
    return fs


def free_mb(path):
    while not os.path.exists(path):
        path = os.path.dirname(path)
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1024 / 1024


# --------------------------------------------------------------------------
# the meeting


def prepare_audio(args, out_dir):
    """An Ogg-Opus file the uploader will take: 16 kHz mono, cut if asked."""
    import soundfile as sf

    src = sf.info(args.audio)
    as_is = (src.format, src.subtype, src.samplerate, src.channels) == (
        "OGG",
        "OPUS",
        16000,
        1,
    )
    if as_is and not args.minutes:
        return args.audio, src.duration
    if src.samplerate != 16000 or src.channels != 1:
        sys.exit(f"{args.audio} is {src.samplerate} Hz, {src.channels} ch; need 16 kHz mono")
    frames = int(args.minutes * 60 * 16000) if args.minutes else -1
    samples, _ = sf.read(args.audio, dtype="int16", frames=frames)
    path = os.path.join(out_dir, "upload.ogg")
    sf.write(path, samples, 16000, format="OGG", subtype="OPUS")
    return path, len(samples) / 16000


def worker_rss_mb(channel):
    """Resident memory of this channel's worker and everything under it --
    the diarization child included. The worker's own processes are the
    ones whose command line carries the property file named after it."""
    parent, rss, roots = {}, {}, set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmdline = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="ignore")
            for line in open(f"/proc/{pid}/status", encoding="utf-8"):
                if line.startswith("PPid:"):
                    parent[pid] = line.split()[1]
                elif line.startswith("VmRSS:"):
                    rss[pid] = int(line.split()[1])
        except OSError:
            continue
        if f"property-{channel}" in cmdline:
            roots.add(pid)

    def under_worker(pid):
        while pid in parent:
            if pid in roots:
                return True
            pid = parent[pid]
        return False

    return sum(kb for pid, kb in rss.items() if under_worker(pid)) / 1024


def run(args, env):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.expanduser(os.path.join(args.out, stamp))
    os.makedirs(out_dir, exist_ok=True)
    channel = f"meeting-check-{stamp}"
    meeting_id = f"check-{stamp}"
    token = args.token or setting(env, "MEETING_AUTH_TOKEN", "")
    auth = {"Authorization": f"Bearer {token}"} if token else {}

    say("audio")
    upload, duration = prepare_audio(args, out_dir)
    ok(f"{upload}: {mmss(duration)} of 16 kHz mono Ogg-Opus, "
       f"{os.path.getsize(upload) / 1024 / 1024:.1f} MB")

    say(f"start {GRAPH} (channel {channel}, timeout {args.timeout} s)")
    status, reply = post_json(
        f"{args.server}/start",
        {
            "request_id": uuid.uuid4().hex,
            "channel_name": channel,
            "graph_name": GRAPH,
            "timeout": args.timeout,
        },
    )
    if not reply or reply.get("code") != "0":
        fail(f"/start answered {status} {reply}")
        return
    ok('/start code "0"')

    try:
        process(args, channel, meeting_id, upload, duration, auth, out_dir)
    finally:
        say("stop")
        status, reply = post_json(
            f"{args.server}/stop",
            {"request_id": uuid.uuid4().hex, "channel_name": channel},
        )
        code = (reply or {}).get("code")
        if code == "0":
            ok("/stop code \"0\"")
        else:
            info(f"/stop answered {status} {reply} (already reaped?)")
        info(f"results in {out_dir}")


def process(args, channel, meeting_id, upload, duration, auth, out_dir):
    say("upload")
    t0 = time.monotonic()
    while request(f"{args.uploader}/meetings", headers=auth, timeout=3)[0] != 200:
        if time.monotonic() - t0 > 60:
            fail(
                f"{args.uploader} did not answer within 60 s of /start",
                "the worker log is /tmp/task_run.log; look for meeting_uploader",
            )
            return
        time.sleep(1)
    ok(f"uploader answered {time.monotonic() - t0:.0f} s after /start")

    fields = {"meeting_id": meeting_id, "title": "board check"}
    if args.speakers:
        fields["speakers"] = str(args.speakers)
    if args.script:
        fields["script"] = args.script
    body, kind = multipart(fields, upload)
    status, reply = request(
        f"{args.uploader}/meeting/upload",
        body,
        {"Content-Type": kind, **auth},
        timeout=120,
    )
    if status != 200:
        fail(f"upload answered {status} {reply[:300]!r}")
        return
    ok(f"upload 200 {reply.decode()}")

    say("process")
    t_upload = time.monotonic()
    last, peak, state, slow = None, 0.0, {}, 0
    deadline = t_upload + args.max_minutes * 60
    while time.monotonic() < deadline:
        status, reply = request(
            f"{args.uploader}/meeting/{meeting_id}", headers=auth, timeout=10
        )
        elapsed = time.monotonic() - t_upload
        if status == SLOW:
            # The uploader shares one Python loop with every extension in the
            # worker; an answer this slow means something is holding it.
            slow += 1
            warn(f"{mmss(elapsed)}  no answer within 10 s ({slow} so far)")
            continue
        if status == GONE:
            fail(
                f"the worker stopped answering {elapsed:.0f} s after the "
                f"upload (timeout was {args.timeout} s)",
                "if that is about the timeout, the keep-alive did not reach "
                "the server: is SERVER_PORT in ai_agents/.env the port the "
                "server listens on? the worker log says why a ping failed",
            )
            return
        state = json.loads(reply)
        peak = max(peak, worker_rss_mb(channel))
        progress = state.get("progress") or {}
        now = (state.get("state"), progress.get("topics_done"))
        if now != last:
            print(
                f"  {mmss(elapsed)}  {state.get('state'):<13}"
                f"{progress.get('topics_done')}/{progress.get('topics_total')}"
                f"   worker {peak:.0f} MB",
                flush=True,
            )
            last = now
        if state.get("state") in FINAL:
            break
        time.sleep(10)
    else:
        fail(f"not finished after {args.max_minutes} minutes")
        return
    if slow:
        fail(
            f"the uploader took over 10 s to answer {slow} time(s)",
            "the worker's shared loop was held; the keep-alive pings wait "
            "on the same loop",
        )
    report(args, meeting_id, state, duration, time.monotonic() - t_upload, peak, auth, out_dir)


def report(args, meeting_id, state, duration, took, peak, auth, out_dir):
    say("result")
    for name in ("record.json", "minutes.txt"):
        status, body = request(
            f"{args.uploader}/meeting/{meeting_id}/{name}", headers=auth
        )
        if status == 200:
            with open(os.path.join(out_dir, name), "wb") as f:
                f.write(body)

    final = state.get("state")
    if final == "archived":
        ok("state archived")
    else:
        fail(f"state {final}: {state.get('error')}")
    record = state.get("record") or {}
    topics = record.get("topics") or []
    failed = [t["id"] for t in topics if t.get("error")]
    summaries = sum(1 for t in topics if t.get("summary"))
    info(f"audio {mmss(duration)}, processed in {mmss(took)} "
         f"({took / max(duration, 1):.2f} x the audio)")
    if failed:
        fail(f"{len(failed)} of {len(topics)} topics failed",
             "see the transcriber and segmenter lines in /tmp/task_run.log")
        for t in topics:
            if t.get("error"):
                info(f"{t['id']}: {t['error'][:200]}")
    else:
        ok(f"{len(topics)} topics, none failed")
    info(f"speakers in the record {record.get('speaker_count')}"
         + (f" (said {args.speakers})" if args.speakers else ""))
    info(f"worker peak memory {peak:.0f} MB")

    if took > args.timeout + 30:
        ok(f"kept alive {took:.0f} s with a {args.timeout} s timeout: "
           "the uploader's ping works")
    else:
        warn(f"processing took {took:.0f} s, not longer than the "
             f"{args.timeout} s timeout; the keep-alive was not exercised")

    if len(topics) == len(failed):
        pass  # nothing to summarise; the failure is already reported
    elif summaries == len(topics) - len(failed):
        ok(f"every topic has a summary ({summaries})")
    else:
        fail(f"{summaries} of {len(topics) - len(failed)} topics have a "
             "summary; an empty one is an LLM turn that failed or timed out",
             "see the llm lines in /tmp/task_run.log")
    if record.get("summary"):
        ok(f"conclusion, {len(record['summary'])} characters")
    else:
        fail("no conclusion")
    info(f"actions {len(record.get('actions') or [])}"
         + (f" ({record['actions_error']})" if record.get("actions_error") else ""))
    if args.script:
        check_script(record, args.script)

    if args.rttm:
        sys.path.insert(0, HERE)
        from probe_meeting_speakers import clip, read_rttm, score

        ref = clip(read_rttm(args.rttm), 0.0, duration)
        hyp = [
            (t["start_s"] + u["start_s"], t["start_s"] + u["end_s"], u["speaker"])
            for t in topics
            for u in t.get("utterances", [])
        ]
        s = score(ref, hyp, duration)
        info(f"wrong person {s['confusion']}% of speech (DER {s['der']}%), "
             f"{s['speakers_found']} found for {s['speakers_ref']}")

    minutes = os.path.join(out_dir, "minutes.txt")
    if os.path.isfile(minutes):
        info("minutes.txt begins:")
        for line in open(minutes, encoding="utf-8").read().splitlines()[:12]:
            print(f"          {line[:100]}")


def main():
    env = dotenv()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--check", action="store_true", help="preflight only")
    p.add_argument("--audio", help="a meeting: .wav or .ogg, 16 kHz mono")
    p.add_argument("--rttm", help="reference speakers, to score the record")
    p.add_argument("--speakers", type=int, help="how many people spoke")
    p.add_argument("--minutes", type=float, help="upload only the first N minutes")
    p.add_argument("--script", choices=("traditional", "simplified"),
                   help="the record's script (default: the board's)")
    p.add_argument(
        "--server",
        default=f"http://127.0.0.1:{setting(env, 'SERVER_PORT', '8081')}",
    )
    p.add_argument("--uploader", default="http://127.0.0.1:8765")
    p.add_argument("--token", help="the uploader's auth_token, if one is set")
    p.add_argument("--timeout", type=int, default=60,
                   help="/start timeout; short on purpose (see above)")
    p.add_argument("--max-minutes", type=float, default=180.0)
    p.add_argument("--out", default="~/meeting_probe/board_check")
    args = p.parse_args()

    preflight(args, env)
    if args.check:
        pass
    elif not args.audio:
        p.error("--audio is needed unless --check")
    elif failures:
        print("\npreflight failed; not starting a meeting", flush=True)
    else:
        run(args, env)

    print(flush=True)
    if failures:
        print(f"{len(failures)} check(s) failed:", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        sys.exit(1)
    print("all checks passed", flush=True)


if __name__ == "__main__":
    main()
