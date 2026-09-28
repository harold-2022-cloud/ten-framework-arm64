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

Three prompts are tried by default and the ladder is the point: one simply
asks for JSON, one supplies the schema, one supplies the schema and asks for
the timestamp to be copied rather than computed. Whichever rung holds first is
the one the pipeline has to climb to. If none holds, the record's shape has to
change -- actions become free text that a person reads, and the design says so
rather than shipping a field that is empty half the time.

Two runs per prompt, not ten. Measured on an N1-655 on 2026-09-28: five runs
of one prompt came back byte-identical, md5 and all, each having spent its own
110 seconds generating rather than being served from a cache. The board decodes
greedily, so repeating an input measures the same answer again. The second run
is there to notice the day that stops being true. What repetition cannot tell
you, and what a later probe should, is whether a prompt survives a different
meeting -- other speakers, other phrasing, a meeting with no todos in it.

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

# What a correct answer contains: for each planted action, the one word that
# makes it actionable, the speaker who owns it, the deadline as it was said,
# and the second of the meeting it was raised at -- read off the second clock
# in the transcript above.
#
# One word each, and it is deliberately the distinguishing one rather than
# the topic. A todo that reads "API 文件" names a subject; nobody can act on
# it. "補上錯誤碼" is the task. Measured on an N1-655 on 2026-09-28 the bare
# prompt produced exactly that -- "API 文件", with the actual instruction
# dropped -- which is a real defect in the output, not a strict test.
GROUND_TRUTH = [
    {
        "key": "測試結果",
        "keywords": ("測試",),  # 給出測試結果
        "owner": "2",
        "due_terms": ("下週二", "下周二"),
        "raised_at_s": 72,  # 01:12
    },
    {
        "key": "API 文件錯誤碼",
        "keywords": ("錯誤碼",),  # not 文件: the task is the error codes
        "owner": "3",
        "due_terms": ("10 月 5", "10月5", "10-05", "10/5"),
        "raised_at_s": 495,  # 08:15
    },
    {
        "key": "客戶驗收標準",
        "keywords": ("驗收",),  # 確認驗收標準
        "owner": "2",
        # Assigned with no deadline ("沒有特別期限，但盡快"), but the person
        # taking it then says 我這週找他們. Both a null and 這週 are correct
        # readings of the transcript, so both score -- measured on an N1-655
        # on 2026-09-28, where the model gave 這週 and was right to.
        "due_terms": ("這週", "本週", "这周"),
        "due_may_be_blank": True,
        "raised_at_s": 888,  # 14:48
    },
]

# Things said but not assigned. An answer that turns these into actions is
# over-producing, which is its own kind of unusable.
DISTRACTORS = ("記憶體", "CI", "自建")

# Every wall clock the transcript shows. A raised_at equal to one of these is
# the wrong clock copied, not a near miss, and saying so beats tolerating it:
# measured on an N1-655 on 2026-09-28 the model answered 14:16 where 14:48
# was wanted, and 14:16 read as 856 seconds fell inside a 60-second tolerance
# of the real 888 by luck. A wrong answer that scores is worse than one that
# does not.
WALL_CLOCKS = frozenset(re.findall(r"\[(\d{2}:\d{2}) /", TRANSCRIPT))

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

# The third rung. Measured on an N1-655 on 2026-09-28, PROMPT_SCHEMA asked
# for raised_at_s as a number and the model wrote
#
#     "raised_at_s": 14*60+2+12 = 862
#
# -- an arithmetic expression with an equals sign, which is not JSON at all,
# so the whole reply was unparseable rather than one field being wrong. The
# sum is wrong twice over besides: 14*60+2+12 is 854, not 862, and it
# multiplied the wall clock 14:03 when the second it wanted was the meeting
# offset 01:12, which is 72. Do not ask a 7B to do arithmetic inside a JSON
# value: have it copy the clock it can see, and convert in Python. That is
# the trade due_raw already makes -- keep the model's characters, parse them
# in code.
#
# Three more things this wording fixes, all of them faults in the prompt
# rather than in the model, each measured in the same run:
#
#   The schema's field descriptions sat in the value positions, and the model
#   copied "整場會議的結論，兩三句" out verbatim as the summary. An example
#   has to look like an answer, so this one is a filled-in example and the
#   instruction to replace it is said out loud.
#
#   "那句話所在行的第二個時間" was not clear enough and it copied the first,
#   giving 14:10 where 08:15 was wanted. The bracket is now spelled out.
#
#   "只列真的被指派給某個人去做的事" excluded 說話人2 volunteering 我下週二
#   前把結果給你, which is a correct reading of the words and the wrong
#   behaviour for a meeting: a todo someone takes on is still a todo.
#
# The one thing left that is genuinely the model's is owner -- it named the
# speaker who did the assigning rather than the person assigned to. Said
# plainly here; whether saying it is enough is what the next run measures.
PROMPT_QUOTED = """\
以下是一場會議的逐字稿。每行開頭是 [牆鐘 / 會議第幾分幾秒]，
例如 [14:10 / 08:15] 表示這句話發生在會議開始後的 08:15。

只輸出一個 JSON 物件，不要有任何其他文字、不要用 markdown 圍欄。
下面是一個填好的範例，照它的格式，但內容要換成這場會議真正的內容：

{
  "summary": "討論了改版時程，並確認了兩份文件的負責人。",
  "actions": [
    {
      "what": "把設定檔的預設值寫進 README",
      "owner": "說話人3",
      "due_raw": "這週五前",
      "raised_at": "12:40"
    }
  ]
}

三件事要特別注意：

一、raised_at 抄該行方括號裡「斜線後面」的那個 MM:SS，原樣抄過來。
    例如 [14:10 / 08:15] 就填 "08:15"。不要抄斜線前面的，
    不要換算成秒，不要寫算式。

二、owner 是「要去做這件事的人」，不是講這句話的人。
    有人說「說話人3，這件事你來處理」，負責人是說話人3。

三、actions 要包含被交辦的事，也要包含自己承諾要做的事。
    有人說「我下週二前給你」，那就是他的待辦。
    但只是討論到、或明講之後再處理的，不要放進 actions。
    沒有講明期限的，due_raw 填 null。

逐字稿：
"""

# The fourth rung, and a different shape: two turns instead of one.
#
# Four rounds on an N1-655 said the same thing from both ends. The bare
# prompt found all three planted actions with the right owners and the right
# deadlines, and then filed them under a key it invented with the timestamps
# in a separate array. Every prompt that pinned the format down took content
# away with it, and monotonically: the quoted prompt at 1719 bytes found two
# of three, and at 2218 bytes -- three more corrections, all of them fair --
# it found one, got no owner right, and invented an action that was never
# assigned. The model extracts, and the model formats. What it does not do is
# both at once, and leaning harder on the format is what costs the content.
#
# So stop asking. One turn reads the meeting, which is the thing it is good
# at, and a second turn reshapes that answer, which is a transcription job
# with no meeting in it. Two turns cost another 30 seconds on a meeting that
# already takes half an hour.
#
# Short on purpose. The lesson of round four is that this model does worse
# the more it is told, so the conversion says what the shape is and stops.
PROMPT_CONVERT = """\
下面是一份會議整理。把它改寫成 JSON，只改格式，不要增加或刪除任何待辦。

只輸出 JSON，不要其他文字：

{
  "summary": "整理裡的結論",
  "actions": [
    {"what": "要做的事", "owner": "說話人N", "due_raw": "期限，沒有就 null",
     "raised_at": "MM:SS"}
  ]
}

raised_at 用資料裡那件事對應的「分:秒」，不是「時:分」。找不到就填 null。

會議整理：
"""

PROMPTS = {
    "plain": PROMPT_PLAIN,
    "schema": PROMPT_SCHEMA,
    "quoted": PROMPT_QUOTED,
    "twostep": PROMPT_PLAIN,  # turn one; turn two is PROMPT_CONVERT
}


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


def ask_twice(args, prompt, out_dir, name, run):
    """Read the meeting, then reshape the reading. Two turns, one result.

    Turn one's reply is saved beside turn two's, because when the pair fails
    it matters which half did. --convert-from skips turn one and feeds a
    saved reply instead: the board is deterministic, so re-reading the same
    transcript costs 108 seconds to produce bytes already on disk.
    """
    if args.convert_from:
        with open(args.convert_from, encoding="utf-8") as handle:
            first = handle.read()
        spent = 0.0
    else:
        first, spent = ask(
            args.host,
            args.port,
            prompt,
            args.model_type,
            args.timeout,
            args.session_id,
        )
        path = os.path.join(out_dir, f"{name}-run{run:02d}-turn1.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(first)

    second_prompt = PROMPT_CONVERT + strip_reasoning(first)
    time.sleep(args.settle)
    raw, more = ask(
        args.host,
        args.port,
        second_prompt,
        args.model_type,
        args.timeout,
        args.session_id,
    )
    return raw, spent + more


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
    """The first balanced {...} in the reply, and why it failed if it did.

    Returns (payload, complaint). Deliberately forgiving about what surrounds
    the object, and for a reason: the pipeline will be just as forgiving, so a
    probe that demanded a bare JSON body would report failures the real code
    recovers from. Markdown fences, a sentence of preamble, a sign-off
    afterwards -- all survive this.

    The complaint carries the characters around the break. "NO JSON" on its
    own sends you to the saved file; "NO JSON near: 14*60+2+12 = 862" is the
    whole diagnosis on one line, and that is a real reply from an N1-655.
    """
    start = text.find("{")
    if start < 0:
        return None, "no opening brace"
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
                blob = text[start : index + 1]
                try:
                    return json.loads(blob), None
                except json.JSONDecodeError as broke:
                    here = max(0, broke.pos - 20)
                    snippet = blob[here : broke.pos + 24].replace("\n", " ")
                    return None, f"near: {snippet.strip()}"
    return None, "no closing brace"


def describe_shape(payload):
    """What the model called things, when it did not call them 'actions'.

    Whether a tolerant parser could ever work turns on one question: does
    the model invent the same key every time, or a different one each run?
    Measured on an N1-655 on 2026-09-28 the bare prompt produced
    "unresolved Matters" -- with a space and a capital -- which is exactly
    the shape of name that will not repeat. Printing the keys per run
    answers that from the summary instead of from eleven saved files.

    Returns (top-level keys, the key holding a list of objects).
    """
    if not isinstance(payload, dict):
        return type(payload).__name__, None
    keys = ", ".join(str(k) for k in list(payload)[:6])
    listy = None
    for key, value in payload.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            listy = str(key)
            break
    return keys, listy


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
        "wrong_clock": 0,
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
            blobs.append(("", "", "", None, ""))
            continue
        what = str(action.get("what") or action.get("task") or "")
        owner = str(action.get("owner") or action.get("who") or "")
        due = " ".join(
            str(action.get(field) or "")
            for field in ("due_raw", "due", "deadline", "when")
        )
        given = (
            action.get("raised_at_s")
            if action.get("raised_at_s") is not None
            else action.get("raised_at")
        )
        blobs.append(
            (what, owner, due, as_seconds(given), str(given or "").strip())
        )

    for truth in GROUND_TRUTH:
        hit = None
        for what, owner, due, when, raw_when in blobs:
            if all(word in what for word in truth["keywords"]):
                hit = (what, owner, due, when, raw_when)
                break
        if hit is None:
            result["missed"].append(truth["key"])
            continue
        result["found"].append(truth["key"])
        _, owner, due, when, raw_when = hit
        if truth["owner"] in owner:
            result["owner_ok"] += 1
        # An item assigned without a deadline is right to have none, so a
        # blank scores wherever the transcript left it open. Otherwise the
        # deadline has to carry the words that were actually said.
        blank = not due.strip()
        nulled = "null" in due.lower() or "none" in due.lower()
        # Saying there is no deadline, in the transcript's own words, is
        # identifying that there is no deadline. Measured on an N1-655 on
        # 2026-09-28 the model answered 沒有特別期限，但盡快 -- a quote of
        # the line that assigned the task -- and scoring that a miss was the
        # probe being wrong, not the model.
        if any(
            phrase in due
            for phrase in ("沒有特別期限", "沒有期限", "無期限", "未指定")
        ):
            nulled = True
        if truth.get("due_may_be_blank") and (blank or nulled):
            result["due_ok"] += 1
        elif not truth["due_terms"] and (blank or nulled):
            result["due_ok"] += 1
        elif any(term in due for term in truth["due_terms"]):
            result["due_ok"] += 1
        if raw_when in WALL_CLOCKS:
            result["wrong_clock"] += 1
        elif (
            when is not None
            and abs(when - truth["raised_at_s"]) <= tolerance_s
        ):
            result["time_ok"] += 1

    for what, _, _, _, _ in blobs:
        for noise in DISTRACTORS:
            if noise in what:
                result["invented"].append(what[:40])
                break
    return result


# ------------------------------------------------------------------- main


def run_variant(name, args, out_dir):
    two_step = name == "twostep"
    prompt = PROMPTS[name] + TRANSCRIPT
    say(f"Prompt '{name}' -- {args.runs} runs")
    kv("prompt bytes", len(prompt.encode("utf-8")))
    if two_step:
        kv("turn 1", "PROMPT_PLAIN, which reads the meeting")
        kv("turn 2", "PROMPT_CONVERT, which only reshapes turn 1's answer")
        if args.convert_from:
            kv("turn 1 taken from", args.convert_from)

    tally = {
        "http_ok": 0,
        "parsed": 0,
        "schema": 0,
        "complete": 0,
        "owner_ok": 0,
        "due_ok": 0,
        "time_ok": 0,
        "invented": 0,
        "wrong_clock": 0,
        "seconds": [],
        "transport_errors": [],
        "invented_keys": [],
    }

    for run in range(1, args.runs + 1):
        if run > 1:
            time.sleep(args.settle)
        try:
            if two_step:
                raw, elapsed = ask_twice(args, prompt, out_dir, name, run)
            else:
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

        payload, complaint = extract_json(answer)
        if payload is None:
            print(
                f"  run {run:>2}  {elapsed:6.1f}s  NO JSON, {complaint} "
                f"-> {os.path.basename(path)}"
            )
            continue
        tally["parsed"] += 1

        marks = score(payload, args.tolerance)
        if not marks["schema"]:
            keys, listy = describe_shape(payload)
            if listy:
                tally["invented_keys"].append(listy)
            print(
                f"  run {run:>2}  {elapsed:6.1f}s  no 'actions' list; "
                f"keys: {keys}"
                + (f"; objects under {listy!r}" if listy else "")
            )
            continue
        tally["schema"] += 1
        tally["owner_ok"] += marks["owner_ok"]
        tally["due_ok"] += marks["due_ok"]
        tally["time_ok"] += marks["time_ok"]
        tally["wrong_clock"] += marks["wrong_clock"]
        if marks["invented"]:
            tally["invented"] += 1
        complete = not marks["missed"]
        if complete:
            tally["complete"] += 1

        flags = []
        if marks["missed"]:
            flags.append("missed " + ",".join(marks["missed"]))
        if marks["wrong_clock"]:
            flags.append(f"wall clock {marks['wrong_clock']}")
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
    if tally["invented_keys"]:
        distinct = sorted(set(tally["invented_keys"]))
        kv("instead it used", ", ".join(repr(k) for k in distinct))
        # One name reused every run could be parsed tolerantly; a different
        # name each run can only be fixed by telling the model the schema.
        kv(
            "same name every time",
            "yes -- a tolerant parser could work"
            if len(distinct) == 1
            else "no -- only the schema prompt can fix this",
        )
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
        if tally["wrong_clock"]:
            kv(
                "copied the wall clock",
                f"{tally['wrong_clock']} -- wanted the MM:SS after the slash",
            )
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
        default=2,
        help="runs per prompt; two, because this board is deterministic --"
        " see the module docstring before raising it",
    )
    parser.add_argument(
        "--convert-from",
        default="",
        help="a saved turn-one reply, so 'twostep' skips re-reading the"
        " transcript; the board is deterministic, so that costs nothing",
    )
    parser.add_argument(
        "--prompts",
        choices=("all", "plain", "schema", "quoted", "twostep"),
        default="all",
        help="'all' is the useful one: it says how much shape the model"
        " has to be told",
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
        default=30,
        help="seconds of slack on the raise time; a copied MM:SS is exact,"
        " so this only ever covers a model that points at a nearby line",
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

    order = ["plain", "schema", "quoted", "twostep"]
    names = order if args.prompts == "all" else [args.prompts]
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

    # The rungs are ordered by how much the model has to be told. The first
    # one that holds is the one the pipeline has to climb to; anything above
    # it is cost with nothing bought.
    advice = {
        "plain": (
            "The bare ask is enough. actions[] can be built as designed,",
            "and the prompt needs nothing beyond what it already says.",
        ),
        "schema": (
            "The schema prompt is required, the bare ask is not enough.",
            "Put PROMPT_SCHEMA's wording into the meeting prompt config",
            "and treat it as load-bearing, not cosmetic.",
        ),
        "quoted": (
            "The schema is required AND the timestamp has to be copied",
            "rather than computed. Use PROMPT_QUOTED's wording, keep the",
            "model's MM:SS as given, and convert to seconds in Python --",
            "the same trade due_raw already makes.",
        ),
        "twostep": (
            "One turn cannot read the meeting and shape the answer at once,",
            "but two can. main_control asks PROMPT_PLAIN for the reading and",
            "PROMPT_CONVERT for the shape, and parses the second reply. That",
            "is one more LLM turn per meeting, about 30 s on a meeting that",
            "already takes half an hour.",
        ),
    }
    print()
    for rung in ("plain", "schema", "quoted", "twostep"):
        if rates.get(rung, 0.0) >= args.bar:
            for line in advice[rung]:
                print(f"  {line}")
            break
    else:
        print("  No prompt held. The record's shape has to change:")
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
