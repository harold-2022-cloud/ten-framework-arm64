#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Can the board's 7B turn a transcript into structured action items?

The meeting-minutes design asks the on-board model for JSON: every action with
a what, an owner, a due date and the second of the meeting it was raised at.
The PRD calls that the largest product risk and says to settle it before any
pipeline code is written, by posting a fake transcript straight at the board.
This is that probe.

It sends a transcript whose answer is known -- three planted actions among
five distractors, each with a distinct owner, a distinct way of expressing a
deadline, and a known timestamp -- and then checks what comes back against
that ground truth, several times over, because one good reply says nothing
about whether the next one parses.

Two prompts are tried by default, and the comparison is the point: one simply
asks for JSON, the other supplies the schema and a worked example. If the bare
ask already holds, the pipeline needs nothing clever. If only the schema
version holds, that prompt is a requirement and belongs in the config. If
neither holds, the record's shape has to change -- actions become free text
that a person reads, and the design says so rather than shipping a field that
is empty half the time.

  python3 tools/ambarella/probe_structured_minutes.py
  python3 tools/ambarella/probe_structured_minutes.py --runs 10
  python3 tools/ambarella/probe_structured_minutes.py --prompts schema
  python3 tools/ambarella/probe_structured_minutes.py --host 192.168.1.50

Speaks the same wire as ambarella_llm2_python: POST to the server root, the
prompt as a text/plain body, Session-Id / Model-Type / Stream-Off / Reset-En
as headers, and the reply's chain-of-thought dropped at the closing think tag.
A probe that invented its own protocol would measure something the pipeline
will never do.

Every run reuses one Session-Id and clears the history with Reset-En, which is
what the extension itself does. A fresh id per run looks safer and is not: the
board runs --max_user 1 and holds a session for 180 seconds after it replies,
so a second id inside that window is a second user and is refused. Measured and
written down in docs/development/board_llm_measurements.zh-TW.md -- one request
per 180 seconds is all a rotating client ever gets.

Stdlib only: it has to run on the board itself, where aiohttp may not be.
Reads only, apart from the replies it saves for you to read.

Exit: 0 if at least one prompt reached the bar, 1 if neither did, 2 if the
board could not be reached at all.
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

BAR = "=" * 78
CLOSE_THINK_TAG = "</think>"

# The two clocks the record actually renders (record.py::_segment_lines), so
# the model sees the format it will really be given, not a tidied-up one.
TRANSCRIPT = """\
[14:02 / 00:35] 說話人1: 我們先看這版的進度，下週要出，測試那邊來得及嗎？
[14:03 / 01:12] 說話人2: 測試要三天，我下週二前把結果給你。
[14:05 / 03:40] 說話人3: 順便問一下，上次那個記憶體的問題還在嗎？
[14:06 / 04:05] 說話人1: 還在，但不影響這版，先放著。
[14:09 / 07:20] 說話人2: 另外文件有點舊，錯誤碼那節根本沒寫。
[14:10 / 08:15] 說話人1: 說話人3，那份 API 文件你在 10 月 5 號前補上錯誤碼。
[14:11 / 09:02] 說話人3: 好，10 月 5 號。
[14:14 / 12:30] 說話人2: 之後可能要考慮把 CI 搬到自建機器，不過那是下一季的事。
[14:16 / 14:48] 說話人1: 還有一件事，說話人2 去跟客戶確認驗收標準，沒有特別期限，但盡快。
[14:17 / 15:20] 說話人2: 收到，我這週找他們。
[14:19 / 17:10] 說話人3: 那今天就到這裡。
"""

# What a correct answer contains. Each planted action carries a keyword that
# has to appear somewhere in `what`, the speaker who owns it, the deadline as
# it was said, and the second of the meeting it was raised at -- read off the
# second clock in the transcript above.
GROUND_TRUTH = [
    {
        "key": "測試結果",
        "keywords": ("測試",),
        "owner": "2",
        "due_terms": ("下週二", "下周二"),
        "raised_at_s": 72,  # 01:12
    },
    {
        "key": "API 文件錯誤碼",
        "keywords": ("文件", "錯誤碼"),
        "owner": "3",
        "due_terms": ("10 月 5", "10月5", "10-05", "10/5"),
        "raised_at_s": 495,  # 08:15
    },
    {
        "key": "客戶驗收標準",
        "keywords": ("驗收", "客戶"),
        "owner": "2",
        "due_terms": (),  # deliberately open-ended: due may be null
        "raised_at_s": 888,  # 14:48
    },
]

# Things said but not assigned. An answer that turns these into actions is
# over-producing, which is its own kind of unusable.
DISTRACTORS = ("記憶體", "CI", "自建")

PROMPT_PLAIN = """\
以下是一場會議的逐字稿，每行開頭的 [時鐘 / 會議第幾分幾秒] 是那句話的時間。

請整理出這場會議的結論，以及所有待辦事項。
待辦要包含：做什麼、誰負責、什麼時候要完成、在會議的第幾秒被提出。
用 JSON 回答。

逐字稿：
"""

PROMPT_SCHEMA = """\
以下是一場會議的逐字稿，每行開頭的 [時鐘 / 會議第幾分幾秒] 是那句話的時間。

只輸出一個 JSON 物件，不要有任何其他文字、不要用 markdown 圍欄。格式如下：

{
  "summary": "整場會議的結論，兩三句",
  "actions": [
    {
      "what": "要做的事",
      "owner": "負責人，用逐字稿裡的說話人編號，例如 說話人2",
      "due_raw": "逐字稿裡提到的期限原話，沒提到就填 null",
      "raised_at_s": 在會議的第幾秒被提出，用整數
    }
  ]
}

只列真的被指派給某個人去做的事。單純討論到、或明說之後再處理的，不要放進 actions。

逐字稿：
"""

PROMPTS = {"plain": PROMPT_PLAIN, "schema": PROMPT_SCHEMA}


def say(title):
    print(f"\n{BAR}\n{title}\n{BAR}")


def kv(key, value):
    print(f"  {key:<30} {value}")


# --------------------------------------------------------------- the wire


def ask(host, port, prompt, model_type, timeout, session_id):
    """One turn, spoken the way ambarella_llm2_python speaks it.

    Non-streaming: the pipeline's summary turns wait for a whole answer
    anyway, and a single body is far easier to save and re-read than a
    reassembled stream. Stream-Off is exactly the case _strip_reasoning
    exists for, so the reply arrives with its chain-of-thought attached.

    One session for every run, with Reset-En clearing the history each time.
    See the module docstring: rotating the id costs 180 seconds a request.
    """
    url = f"http://{host}:{port}/"
    request = urllib.request.Request(
        url,
        data=prompt.encode("utf-8"),
        method="POST",
        headers={
            "Session-Id": session_id,
            "Model-Type": str(model_type),
            "Stream-Off": "1",
            "Reset-En": "1",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return raw, time.time() - started


def strip_reasoning(text):
    """Drop everything before the closing think tag, as the extension does.

    A reply with no tag is returned whole: absent the delimiter there is
    nothing to say the text is reasoning, and swallowing it would lose the
    answer outright.
    """
    _, delimiter, answer = text.partition(CLOSE_THINK_TAG)
    return (answer if delimiter else text).strip()


# ------------------------------------------------------------- the parsing


def extract_json(text):
    """The first balanced {...} in the reply, or None.

    Deliberately forgiving, and for a reason: the pipeline will be just as
    forgiving, so a probe that demanded a bare JSON body would report
    failures the real code recovers from. Markdown fences, a sentence of
    preamble, a sign-off afterwards -- all survive this.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def as_seconds(value):
    """An int, a float, "888", or "14:48" -- all mean the same second."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    match = re.fullmatch(r"(\d+):(\d{1,2})", text)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    return None


def score(payload, tolerance_s):
    """One reply against the ground truth.

    Returns a dict of what held and what did not. Nothing here is fatal on
    its own -- the verdict at the end weighs them.
    """
    result = {
        "schema": False,
        "found": [],
        "missed": [],
        "owner_ok": 0,
        "due_ok": 0,
        "time_ok": 0,
        "invented": [],
        "n_actions": 0,
    }
    if not isinstance(payload, dict):
        return result
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return result
    result["schema"] = True
    result["n_actions"] = len(actions)

    blobs = []
    for action in actions:
        if not isinstance(action, dict):
            blobs.append(("", "", "", None))
            continue
        what = str(action.get("what") or action.get("task") or "")
        owner = str(action.get("owner") or action.get("who") or "")
        due = " ".join(
            str(action.get(field) or "")
            for field in ("due_raw", "due", "deadline", "when")
        )
        when = as_seconds(
            action.get("raised_at_s")
            if action.get("raised_at_s") is not None
            else action.get("raised_at")
        )
        blobs.append((what, owner, due, when))

    for truth in GROUND_TRUTH:
        hit = None
        for what, owner, due, when in blobs:
            if all(word in what for word in truth["keywords"]):
                hit = (what, owner, due, when)
                break
        if hit is None:
            result["missed"].append(truth["key"])
            continue
        result["found"].append(truth["key"])
        _, owner, due, when = hit
        if truth["owner"] in owner:
            result["owner_ok"] += 1
        # An open-ended item is right to have no deadline; anything else has
        # to carry the words that were actually said.
        if not truth["due_terms"]:
            blank = not due.strip()
            nulled = "null" in due.lower() or "none" in due.lower()
            if blank or nulled:
                result["due_ok"] += 1
        elif any(term in due for term in truth["due_terms"]):
            result["due_ok"] += 1
        if when is not None and abs(when - truth["raised_at_s"]) <= tolerance_s:
            result["time_ok"] += 1

    for what, _, _, _ in blobs:
        for noise in DISTRACTORS:
            if noise in what:
                result["invented"].append(what[:40])
                break
    return result


# ------------------------------------------------------------------- main


def run_variant(name, args, out_dir):
    prompt = PROMPTS[name] + TRANSCRIPT
    say(f"Prompt '{name}' -- {args.runs} runs")
    kv("prompt bytes", len(prompt.encode("utf-8")))

    tally = {
        "http_ok": 0,
        "parsed": 0,
        "schema": 0,
        "complete": 0,
        "owner_ok": 0,
        "due_ok": 0,
        "time_ok": 0,
        "invented": 0,
        "seconds": [],
        "transport_errors": [],
    }

    for run in range(1, args.runs + 1):
        if run > 1:
            time.sleep(args.settle)
        try:
            raw, elapsed = ask(
                args.host,
                args.port,
                prompt,
                args.model_type,
                args.timeout,
                args.session_id,
            )
        except (urllib.error.URLError, OSError, TimeoutError) as failure:
            print(f"  run {run:>2}  transport failed: {failure}")
            tally["transport_errors"].append(str(failure))
            continue

        tally["http_ok"] += 1
        tally["seconds"].append(elapsed)
        answer = strip_reasoning(raw)

        path = os.path.join(out_dir, f"{name}-run{run:02d}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(raw)

        payload = extract_json(answer)
        if payload is None:
            print(
                f"  run {run:>2}  {elapsed:6.1f}s  NO JSON        "
                f"({len(answer)} chars) -> {os.path.basename(path)}"
            )
            continue
        tally["parsed"] += 1

        marks = score(payload, args.tolerance)
        if not marks["schema"]:
            print(
                f"  run {run:>2}  {elapsed:6.1f}s  JSON, no 'actions' list "
                f"-> {os.path.basename(path)}"
            )
            continue
        tally["schema"] += 1
        tally["owner_ok"] += marks["owner_ok"]
        tally["due_ok"] += marks["due_ok"]
        tally["time_ok"] += marks["time_ok"]
        if marks["invented"]:
            tally["invented"] += 1
        complete = not marks["missed"]
        if complete:
            tally["complete"] += 1

        flags = []
        if marks["missed"]:
            flags.append("missed " + ",".join(marks["missed"]))
        if marks["invented"]:
            flags.append(f"invented {len(marks['invented'])}")
        print(
            f"  run {run:>2}  {elapsed:6.1f}s  "
            f"{len(marks['found'])}/3 actions  "
            f"owner {marks['owner_ok']}/3  "
            f"due {marks['due_ok']}/3  "
            f"time {marks['time_ok']}/3"
            + ("   " + "; ".join(flags) if flags else "")
        )

    return tally


def report(name, tally, runs):
    say(f"Prompt '{name}' -- result")
    if not tally["http_ok"]:
        kv("reached the board", "never")
        return 0.0
    seconds = tally["seconds"]
    kv("runs that answered", f"{tally['http_ok']}/{runs}")
    kv("median seconds", f"{sorted(seconds)[len(seconds) // 2]:.1f}")
    kv("a JSON object came back", f"{tally['parsed']}/{runs}")
    kv("it had an actions list", f"{tally['schema']}/{runs}")
    kv("all 3 planted actions found", f"{tally['complete']}/{runs}")
    # Denominator is the actions the runs could have got right: three per
    # run that produced a list at all. A run that never got that far is
    # already counted above, and counting it again here would read as a
    # field problem when it was a format problem.
    graded = tally["schema"] * len(GROUND_TRUTH)
    if graded:
        kv("owner correct", f"{tally['owner_ok']}/{graded}")
        kv("deadline correct", f"{tally['due_ok']}/{graded}")
        kv("raised-at within tolerance", f"{tally['time_ok']}/{graded}")
    kv("runs that invented an action", tally["invented"])
    return tally["complete"] / runs if runs else 0.0


def main():
    parser = argparse.ArgumentParser(
        description="Does the board's 7B produce usable structured minutes?"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--model-type",
        type=int,
        default=9,
        help="9 is deepseek_7B, the model the kit ships pre-converted",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="runs per prompt; one good reply proves nothing about the next",
    )
    parser.add_argument(
        "--prompts",
        choices=("both", "plain", "schema"),
        default="both",
        help="'both' is the useful one: it says whether the schema is needed",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="per request; the pipeline's own summary_timeout_s is 180",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=5.0,
        help="pause between runs; the board serves one user at a time",
    )
    parser.add_argument(
        "--tolerance",
        type=int,
        default=60,
        help="seconds of slack on raised_at_s before it counts as wrong",
    )
    parser.add_argument(
        "--bar",
        type=float,
        default=0.8,
        help="fraction of runs that must find all 3 actions to pass",
    )
    parser.add_argument(
        "--min-answered",
        type=float,
        default=0.6,
        help="fraction of requests that must reach the board before any"
        " verdict on the model is printed at all",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="reused by every run; random non-zero integer when empty",
    )
    parser.add_argument("--out", default="/tmp/structured_minutes_probe")
    args = parser.parse_args()

    # The board parses Session-Id numerically, so it has to be decimal digits
    # and must not come out as zero -- the same rule the extension enforces.
    if args.session_id:
        if not args.session_id.isdigit() or int(args.session_id) == 0:
            parser.error("--session-id must be a non-zero decimal integer")
    else:
        args.session_id = str(random.randint(1, 2**31 - 1))

    names = ["plain", "schema"] if args.prompts == "both" else [args.prompts]
    os.makedirs(args.out, exist_ok=True)

    say("0. Context")
    kv("target", f"http://{args.host}:{args.port}/")
    kv("model type", args.model_type)
    kv("prompts", ", ".join(names))
    kv("runs per prompt", args.runs)
    kv("session id (reused)", args.session_id)
    kv("planted actions", len(GROUND_TRUTH))
    kv("replies saved to", args.out)
    # A JSON answer of this shape is 300-500 characters; at the 8-11 chars/s
    # measured on this board that is roughly a minute of generation, before
    # the reasoning the model does first.
    calls = len(names) * args.runs
    kv(
        "expect roughly",
        f"{calls} calls, {calls * (75 + args.settle) / 60:.0f}-"
        f"{calls * (150 + args.settle) / 60:.0f} min",
    )

    rates = {}
    tallies = {}
    for name in names:
        tallies[name] = run_variant(name, args, args.out)
        rates[name] = report(name, tallies[name], args.runs)

    # A board that mostly did not answer has told us nothing about the model,
    # and the advice below would be actively wrong -- it would send someone to
    # redesign the record over what is probably a stopped or wedged demo
    # server. One reply out of ten is a transport result, not a model result.
    answered = sum(t["http_ok"] for t in tallies.values())
    attempted = len(names) * args.runs
    if answered < attempted * args.min_answered:
        say("Nothing conclusive was measured")
        print(
            f"  Only {answered} of {attempted} requests reached the board,"
            f" under the {args.min_answered:.0%} this needs to judge the"
            " model."
        )
        print("  What came back says nothing about whether the model can")
        print("  produce structured minutes.\n")
        seen = []
        for tally in tallies.values():
            for error in tally["transport_errors"]:
                if error not in seen:
                    seen.append(error)
        for error in seen:
            print(f"    {error}")
        print(
            "\n  'Remote end closed connection' after one good reply is the"
            "\n  board's own limit, not a crash: --max_user 1 holds a session"
            "\n  for 180 s after it answers. This probe reuses one Session-Id"
            "\n  to stay inside that, so seeing it here means something else"
            "\n  is holding the slot -- a worker mid-meeting, another probe,"
            "\n  or a session the last run never released. Wait 180 s and"
            "\n  retry before anything more drastic."
        )
        print("\n  Check that the LLM demo server is up and listening:")
        print(f"    curl -sS -X POST --url http://{args.host}:{args.port}/ \\")
        print("      -H 'Session-Id: 1234' -H 'Model-Type: "
              f"{args.model_type}' -H 'Stream-Off: 1' \\")
        print("      -H 'Content-Type: text/plain; charset=utf-8' -d 'Hello'")
        print("\n  tools/ambarella/check_llm_board.sh checks the whole setup,")
        print("  and probe_llm_wire.py diagnoses a server that answers badly.")
        return 2

    say("Verdict")
    for name in names:
        state = "PASS" if rates[name] >= args.bar else "FAIL"
        kv(
            name,
            f"{rates[name]:.0%} complete   {state} (bar {args.bar:.0%})",
        )

    print()
    if rates.get("plain", 0.0) >= args.bar:
        print("  The bare ask is enough. actions[] can be built as designed,")
        print("  and the prompt needs nothing beyond what it already says.")
    elif rates.get("schema", 0.0) >= args.bar:
        print("  The schema prompt is required, the bare ask is not enough.")
        print("  Put PROMPT_SCHEMA's wording into the meeting prompt config")
        print("  and treat it as load-bearing, not cosmetic.")
    else:
        print("  Neither prompt held. The record's shape has to change:")
        print("  keep the model's prose in `summary`, leave `actions` empty,")
        print("  and let a person pull the todos out of the transcript --")
        print("  which is exactly the fallback the design already carries.")
        print("  Do not ship a field that is right half the time.")
    print(f"\n  Every reply is in {args.out} -- read the failures.")

    return 0 if any(rate >= args.bar for rate in rates.values()) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
