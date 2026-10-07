#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""What the board's LLM writes for one meeting topic, and how long it takes.

  python3.12 tools/ambarella/probe_meeting_summary.py              # topic 1
  python3.12 tools/ambarella/probe_meeting_summary.py --topic 2
  python3.12 tools/ambarella/probe_meeting_summary.py --record path/record.json
  python3.12 tools/ambarella/probe_meeting_summary.py --variants   # compare prompts
  python3.12 tools/ambarella/probe_meeting_summary.py --conclusions  # who does what
  python3.12 tools/ambarella/probe_meeting_summary.py --garbling     # saved bytes

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

--variants asks the same topic several ways and tabulates them. The first
run showed why the prompt then shipped was slow: thinking was bypassed,
the first character came at 2.9 s and the model wrote 10 characters a
second -- but it wrote 3419 of them, copying the 1601-character transcript
line by line, twice, rather than summarising it. "Mark the time and the
speaker" reads to a 7B as "list every timestamped line". The board's model
decodes greedily, so one run per variant is the whole answer.

--conclusions asks for the meeting's conclusion three ways. With the
summary prompt fixed, the board's first full run wrote action items owned
by "李老师" and "张老师" -- people who are not in the meeting. The
conclusion was asked "who does what" from summaries that name nobody.

Every answer is also decoded twice, and both counts of U+FFFD reported:
as ambarella_llm2_python reads the stream (the whole body decoded, then
split into events) and with each event's bytes joined before decoding.
The daemon sends one character per event; a character whose UTF-8 bytes
arrive in two events breaks under the first and survives the second. The
first broken event is printed in hex, so the wire itself says which.

Run it with nothing else using the LLM: the daemon serves one session at a
time and refuses a second by closing the connection. Everything is also
saved under --out.
"""

import argparse
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
MENDER = os.path.join(
    REPO,
    "ai_agents",
    "agents",
    "ten_packages",
    "extension",
    "ambarella_llm2_python",
    "utf8_mend.py",
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


def control_config():
    """meeting_control_python's own config.py, so the prompts are the
    shipped ones and not copies."""
    spec = importlib.util.spec_from_file_location("control_config", CONTROL_CONFIG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MeetingControlConfig()


def segment_prompt():
    return control_config().segment_prompt


def build_prompt(
    record_path, topic_no, instruction=None, times=True, label="說話人"
):
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
        (f"[{mmss(topic['start_s'] + u['start_s'])}] " if times else "")
        + f"{label}{u['speaker'] + 1}：{u['text']}"
        for u in topic["utterances"]
    ]
    head = segment_prompt() if instruction is None else instruction
    return head + "\n".join(lines), topic


def new_session_id():
    # The daemon parses this as a number and refuses zero.
    return str(random.randint(100_000_000, 999_999_999))


class Reply:
    """One answer off the wire, kept as bytes and decoded both ways."""

    def __init__(self, raw, payloads, first, total, ended):
        self.raw, self.payloads = raw, payloads
        self.first, self.total, self.ended = first, total, ended

    @property
    def as_extension_reads(self):
        """ambarella_llm2_python's way: decode the body, then split it."""
        body = self.raw.decode("utf-8", "replace")
        values = []
        for line in body.split("\n"):
            line = line.rstrip("\r")
            if line.startswith("data:"):
                value = line[5:]
                values.append(value[1:] if value.startswith(" ") else value)
        return unescape("".join(v for v in values if v not in DONE))

    @property
    def joined_bytes(self):
        """Each event's bytes joined first, then decoded."""
        return unescape(b"".join(self.payloads).decode("utf-8", "replace"))

    @property
    def mended(self):
        """As ambarella_llm2_python reads it now: its own utf8_mend.py
        puts back the characters the daemon breaks across two events."""
        spec = importlib.util.spec_from_file_location("utf8_mend", MENDER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return unescape(module.mend(self.payloads).decode("utf-8", "replace"))


def event_payloads(raw):
    """The data: values, as bytes. Splitting on b"\\n" is safe: no byte of a
    multi-byte UTF-8 character is 0x0A."""
    out = []
    for line in raw.split(b"\n"):
        line = line.rstrip(b"\r")
        if line.startswith(b"data:"):
            value = line[5:]
            out.append(value[1:] if value.startswith(b" ") else value)
    return out


def ask(base_url, prompt, max_seconds, raw_path, quiet=False, session_id=None):
    """Stream one answer into a Reply."""
    url = urllib.parse.urlparse(base_url)
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=120)
    headers = {
        "Session-Id": session_id or new_session_id(),
        "Model-Type": "9",
        "Stream-Off": "0",
        "Reset-En": "1",
        "Content-Type": "text/plain; charset=utf-8",
    }
    t0 = time.monotonic()
    conn.request("POST", url.path or "/", prompt.encode("utf-8"), headers)
    resp = conn.getresponse()
    if not quiet:
        info(f"HTTP {resp.status} after {time.monotonic() - t0:.1f} s")

    raw = bytearray()
    first, ended, next_report = None, "connection closed", t0 + 15
    while True:
        if time.monotonic() - t0 > max_seconds:
            ended = f"stopped by this probe at {max_seconds:.0f} s"
            break
        chunk = resp.read1(4096)
        if not chunk:
            break
        raw += chunk
        payloads = event_payloads(bytes(raw))
        if payloads and first is None:
            first = time.monotonic() - t0
        if payloads and payloads[-1].decode("ascii", "replace") in DONE:
            ended = payloads[-1].decode("ascii")
            break
        now = time.monotonic()
        if now >= next_report and not quiet:
            info(f"{mmss(now - t0)}  {len(payloads)} events so far")
            next_report = now + 15
    conn.close()
    with open(raw_path, "wb") as f:
        f.write(raw)
    payloads = [
        v for v in event_payloads(bytes(raw))
        if v.decode("ascii", "replace") not in DONE
    ]
    return Reply(bytes(raw), payloads, first, time.monotonic() - t0, ended)


def first_broken_event(payloads):
    """The first event whose bytes are not UTF-8 on their own, with three
    events either side, in hex -- or None."""
    for i, value in enumerate(payloads):
        try:
            value.decode("utf-8")
        except UnicodeDecodeError:
            around = payloads[max(0, i - 3) : i + 4]
            return "\n".join(
                f"          event {max(0, i - 3) + k}: {v.hex(' ')}  {v!r}"
                for k, v in enumerate(around)
            )
    return None


def unescape(text):
    return text.replace("<SP>", " ").replace("<NL>", "\n")


# Each tried on the same topic. Short on purpose: on this 7B every addition
# to a prompt cost content (PRD Part 2).
VARIANTS = [
    ("prose", True, "說話人",
     "以下是一段會議的逐字稿。用中文寫三到五句話，總結這段在討論什麼、"
     "做了什麼決定、誰要做什麼。不要逐句複述原文。\n\n"),
    ("prose-notimes", False, "說話人",
     "以下是一段會議的逐字稿。用中文寫三到五句話，總結這段在討論什麼、"
     "做了什麼決定、誰要做什麼。不要逐句複述原文。\n\n"),
    ("short150", False, "說話人",
     "以下是一段會議的逐字稿。用不超過150字總結重點，不要複述原文。\n\n"),
    ("points3", False, "說話人",
     "以下是一段會議的逐字稿。列出這段最重要的三個重點，每點一句話，"
     "不要引用原文。\n\n"),
    ("simplified", False, "说话人",
     "以下是一段会议的逐字稿。用中文写三到五句话，总结这段在讨论什么、"
     "做了什么决定、谁要做什么。不要逐句复述原文。\n\n"),
]


def copied_share(answer, transcript, run=10):
    """How much of the answer is the transcript itself: the share of its
    characters inside some run of `run` or more that occurs verbatim in the
    transcript. Markup, timestamps and speaker labels are not in the
    transcript, so a faithful copy scores high but not 100%."""
    covered = [False] * len(answer)
    for i in range(len(answer) - run + 1):
        if answer[i : i + run] in transcript:
            for j in range(i, i + run):
                covered[j] = True
    return sum(covered) / max(len(answer), 1)


def ask_patiently(base_url, prompt, max_seconds, raw_path, session_id):
    """ask(), again after a refusal. The daemon serves one session at a
    time and refuses by closing the connection with no response at all."""
    for attempt in range(1, 5):
        try:
            return ask(base_url, prompt, max_seconds, raw_path, True, session_id)
        except (http.client.RemoteDisconnected, ConnectionError) as err:
            info(f"refused ({type(err).__name__}), attempt {attempt} of 4; "
                 "waiting 30 s")
            time.sleep(30)
    return Reply(b"", [], None, 0.0, "refused four times")


def run_variants(args, record, base_url, out_dir, session_id):
    rows = []
    for name, times, label, instruction in VARIANTS:
        say(f"variant {name}")
        prompt, topic = build_prompt(record, args.topic, instruction, times, label)
        transcript = "\n".join(u["text"] for u in topic["utterances"])
        reply = ask_patiently(
            base_url,
            prompt,
            args.variant_seconds,
            os.path.join(out_dir, f"{name}.sse"),
            session_id,
        )
        total, ended = reply.total, reply.ended
        answer = reply.mended
        with open(os.path.join(out_dir, f"{name}.txt"), "w", encoding="utf-8") as f:
            f.write(prompt + "\n\n----- answer -----\n" + answer)
        row = {
            "name": name,
            "prompt": len(prompt),
            "seconds": total,
            "finished": ended in DONE,
            "chars": len(answer),
            "copied": copied_share(answer, transcript),
            "garbled": reply.as_extension_reads.count("\ufffd"),
            "garbled_joined": reply.joined_bytes.count("\ufffd"),
            "garbled_mended": answer.count("\ufffd"),
            "answer": answer,
            "reply": reply,
        }
        rows.append(row)
        info(f"{total:.0f} s, {len(answer)} characters, "
             f"{'finished' if row['finished'] else ended}, "
             f"{row['copied']:.0%} copied, U+FFFD {row['garbled']} as read "
             f"before, {row['garbled_mended']} mended")
        # The daemon holds a finished session briefly; do not crowd it.
        time.sleep(5)

    say("comparison")
    print(f"  {'variant':<15}{'prompt':>7}{'secs':>6}{'chars':>7}"
          f"{'copied':>8}{'FFFD before':>13}{'mended':>8}  finished",
          flush=True)
    for r in rows:
        print(f"  {r['name']:<15}{r['prompt']:>7}{r['seconds']:>6.0f}"
              f"{r['chars']:>7}{r['copied']:>8.0%}{r['garbled']:>13}"
              f"{r['garbled_mended']:>8}  "
              f"{'yes' if r['finished'] else 'no'}", flush=True)
    for r in rows:
        if garbling(r["reply"], quiet_if_clean=True):
            break
    for r in rows:
        say(f"{r['name']}, first 400 characters")
        print(r["answer"][:400], flush=True)


def garbling(reply, quiet_if_clean=False):
    """Both decodings' U+FFFD counts and, if any, the first broken event.
    Returns whether there was something to show."""
    old = reply.as_extension_reads.count("\ufffd")
    new = reply.joined_bytes.count("\ufffd")
    fixed = reply.mended.count("\ufffd")
    broken = first_broken_event(reply.payloads)
    if quiet_if_clean and not broken:
        return False
    say("garbling")
    info(f"U+FFFD as ambarella_llm2_python reads the stream: {old}")
    info(f"U+FFFD with each event's bytes joined first:      {new}")
    info(f"U+FFFD mended by ambarella_llm2_python/utf8_mend: {fixed}")
    if broken:
        info("first event that is not UTF-8 by itself, in hex:")
        print(broken, flush=True)
    else:
        info("every event is UTF-8 by itself")
    return True


CONCLUSIONS = [
    # name, conclusion prompt (None: the one meeting_control_python ships)
    ("shipped", None),
    ("no-owner",
     "以下是一场会议各段的总结。用中文写出：一、这场会议的结论；"
     "二、待办事项。不要逐句复述原文。\n\n"),
    ("copy-numbers",
     "以下是一场会议各段的总结。用中文写出：一、这场会议的结论；"
     "二、待办事项。总结里写了是哪位说话人的，照写那个编号；"
     "没写的，不写负责人。不要逐句复述原文。\n\n"),
]


def people_named(text):
    """Titles and owners that are not a speaker number -- invented, since
    the record names nobody."""
    import re  # pylint: disable=import-outside-toplevel

    titled = re.findall(
        r"[\u4e00-\u9fff]{1,2}(?:老师|经理|主任|园长|校长|先生|女士|总监|同学)",
        text,
    )
    owners = [
        o for o in re.findall(r"由([^\s，。、：]{1,6}?)负责", text)
        if not o.startswith(("说话人", "說話人"))
    ]
    # "负责人1", "负责人2", ...: numbered owners that are no speaker. On the
    # 38-minute run the conclusion numbered its five items' owners 1 to 5.
    placeholders = re.findall(r"负责人\s*[：:]?\s*\d+", text)
    found = {n[1:] if n.startswith("由") else n for n in titled + owners}
    return sorted(found | {re.sub(r"\s|[：:]", "", p) for p in placeholders})


def run_conclusions(args, record, base_url, out_dir, session_id):
    """The shipped summaries of every topic, then the meeting's conclusion
    asked each way in CONCLUSIONS."""
    with open(record, encoding="utf-8") as f:
        topics = [t for t in json.load(f)["topics"] if not t.get("error")]
    summaries = []
    for n in range(1, len(topics) + 1):
        say(f"summary of topic {n}")
        prompt, _ = build_prompt(record, n, segment_prompt(), False, "说话人")
        reply = ask_patiently(
            base_url, prompt, args.variant_seconds,
            os.path.join(out_dir, f"summary-{n}.sse"), session_id,
        )
        text = reply.mended.strip()
        summaries.append(text)
        info(f"{reply.total:.0f} s, {len(text)} characters, "
             f"{text.count('说话人')} speaker numbers")
        print(text, flush=True)
        time.sleep(5)

    rows = []
    for name, instruction in CONCLUSIONS:
        say(f"conclusion {name}")
        body = "\n\n".join(
            f"第{n}段：{s}" for n, s in enumerate(summaries, 1) if s
        )
        prompt = (instruction or control_config().meeting_prompt) + body
        reply = ask_patiently(
            base_url, prompt, args.variant_seconds,
            os.path.join(out_dir, f"{name}.sse"), session_id,
        )
        text = reply.mended.strip()
        with open(os.path.join(out_dir, f"{name}.txt"), "w", encoding="utf-8") as f:
            f.write(prompt + "\n\n----- answer -----\n" + text)
        rows.append((name, reply.total, len(text), people_named(text),
                     text.count("说话人"), text))
        info(f"{reply.total:.0f} s, {len(text)} characters")
        time.sleep(5)

    say("comparison")
    print(f"  {'conclusion':<14}{'secs':>6}{'chars':>7}{'speaker no.':>13}"
          "  invented owners (none are in the meeting)", flush=True)
    for name, secs, chars, people, numbers, _ in rows:
        print(f"  {name:<14}{secs:>6.0f}{chars:>7}{numbers:>13}  "
              f"{'、'.join(people) or '-'}", flush=True)
    for name, _, _, _, _, text in rows:
        say(f"{name}, in full")
        print(text, flush=True)


def unmended_event(payloads):
    """The first event after which the mended output stops being UTF-8,
    or None. Mended output can trail its event by one, so look around."""
    spec = importlib.util.spec_from_file_location("utf8_mend", MENDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    mender = module.Mender()
    for i, value in enumerate(payloads):
        try:
            mender.feed(value).decode("utf-8")
        except UnicodeDecodeError:
            return i
    return None


def run_garbling(directory):
    """The garbling report for every answer a past run saved, from its
    bytes -- no question is asked. The worst first."""
    files = sorted(glob.glob(os.path.join(directory, "*.sse")))
    if not files:
        sys.exit(f"no saved answers (*.sse) in {directory}")
    replies = []
    for path in files:
        with open(path, "rb") as f:
            raw = f.read()
        payloads = [
            v for v in event_payloads(raw)
            if v.decode("ascii", "replace") not in DONE
        ]
        replies.append((os.path.basename(path)[:-4], Reply(raw, payloads, 0, 0, "")))
    say(f"saved answers in {directory}")
    print(f"  {'answer':<22}{'events':>8}{'as read':>9}{'joined':>8}"
          f"{'mended':>8}{'events not UTF-8':>18}", flush=True)
    for name, r in replies:
        broken = 0
        for v in r.payloads:
            try:
                v.decode("utf-8")
            except UnicodeDecodeError:
                broken += 1
        print(f"  {name:<22}{len(r.payloads):>8}"
              f"{r.as_extension_reads.count(chr(0xFFFD)):>9}"
              f"{r.joined_bytes.count(chr(0xFFFD)):>8}"
              f"{r.mended.count(chr(0xFFFD)):>8}{broken:>18}", flush=True)
    def damage(reply):
        return max(
            reply.as_extension_reads.count(chr(0xFFFD)),
            reply.joined_bytes.count(chr(0xFFFD)),
        )

    worst = [nr for nr in sorted(replies, key=lambda nr: -damage(nr[1])) if damage(nr[1])]
    if not worst:
        say("no U+FFFD in any saved answer")
    for name, r in worst[:2]:
        say(f"{name}: where it breaks")
        mended = r.mended
        at = mended.find(chr(0xFFFD))
        info("mended: " + (repr(mended[max(0, at - 15) : at + 15])
                           if at >= 0 else "no U+FFFD left"))
        left = unmended_event(r.payloads)
        if left is not None:
            info("first break the mender leaves, in hex:")
            for i in range(max(0, left - 4), min(len(r.payloads), left + 4)):
                v = r.payloads[i]
                mark = "  <-" if i == left else ""
                print(f"          event {i}: {v.hex(' '):<24} {v!r}{mark}",
                      flush=True)
            continue  # the hex below is for breaks the mender repaired
        text = r.joined_bytes
        if chr(0xFFFD) not in text:
            text = r.as_extension_reads  # joined repaired it; show the break
        at = text.find(chr(0xFFFD))
        if at < 0:
            info("no U+FFFD")
            continue
        info(f"text around the first U+FFFD: {text[max(0, at - 15):at + 15]!r}")
        # The events that carry that stretch of text, in hex.
        upto, start = 0, None
        for i, v in enumerate(r.payloads):
            upto += len(unescape(v.decode("utf-8", "replace")))
            if upto > at - 6 and start is None:
                start = i
            if start is not None and i >= start + 12:
                break
        for i in range(start or 0, min(len(r.payloads), (start or 0) + 13)):
            v = r.payloads[i]
            print(f"          event {i}: {v.hex(' '):<24} {v!r}", flush=True)


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
    p.add_argument("--variants", action="store_true",
                   help="compare the prompt variants instead")
    p.add_argument("--conclusions", action="store_true",
                   help="compare the conclusion prompts instead")
    p.add_argument("--garbling", nargs="?", const="",
                   help="report U+FFFD from a past run's saved bytes "
                        "(default: the latest run); asks nothing")
    p.add_argument("--variant-seconds", type=float, default=300.0,
                   help="cap per variant")
    p.add_argument("--out", default="~/meeting_probe/summary_probe")
    args = p.parse_args()

    if args.garbling is not None:
        runs = sorted(glob.glob(os.path.join(os.path.expanduser(args.out), "*")))
        run_garbling(args.garbling or (runs[-1] if runs else args.out))
        return

    record = args.record or (sorted(glob.glob(os.path.expanduser(
        "~/meeting_probe/board_check/*/record.json"))) or [None])[-1]
    if not record:
        sys.exit("no record.json: run check_meeting_board.py first, or pass --record")
    out_dir = os.path.join(os.path.expanduser(args.out), time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    thinking()
    base_url = setting("AMBARELLA_LLM_BASE_URL", "http://127.0.0.1:8080")

    if args.variants or args.conclusions:
        # One session for the whole run, cleared by Reset-En each time --
        # what probe_structured_minutes.py does, and what the graph's llm
        # node does. A fresh Session-Id per question was refused by the
        # daemon on the third.
        session_id = new_session_id()
        if args.variants:
            run_variants(args, record, base_url, out_dir, session_id)
        if args.conclusions:
            run_conclusions(args, record, base_url, out_dir, session_id)
        print(f"\nsaved each prompt and answer in {out_dir}", flush=True)
        return

    say(f"prompt: topic {args.topic} of {record}")
    prompt, topic = build_prompt(record, args.topic)
    with open(os.path.join(out_dir, "prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)
    info(f"{len(prompt)} characters, {len(topic['utterances'])} lines, "
         f"topic {mmss(topic['start_s'])}-{mmss(topic['end_s'])}")

    say(f"asking {base_url} (no limit but --max-seconds {args.max_seconds:.0f})")
    reply = ask(
        base_url, prompt, args.max_seconds, os.path.join(out_dir, "raw.sse")
    )
    answer = reply.mended
    with open(os.path.join(out_dir, "answer.txt"), "w", encoding="utf-8") as f:
        f.write(answer)
    report(answer, reply.first, reply.total, reply.ended, len(prompt))
    garbling(reply)
    print(f"\nsaved prompt.txt, raw.sse, answer.txt in {out_dir}", flush=True)


if __name__ == "__main__":
    main()
