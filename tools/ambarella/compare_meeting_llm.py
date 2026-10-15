#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Same meeting, same prompts, two LLMs: the board's DeepSeek 7B (the
vendor's daemon, 8080) and Qwen3.5-4B (cooper-vllm, 8000). They cannot run
at once, so each answers in a round of its own and the rounds are compared
afterwards.

  python3.12 tools/ambarella/compare_meeting_llm.py run --label deepseek-7b --out DIR
  python3.12 tools/ambarella/compare_meeting_llm.py run --label qwen35-4b --out DIR \\
      --openai http://127.0.0.1:8000/v1 --model qwen3.5_4B
  python3.12 tools/ambarella/compare_meeting_llm.py compare DIR
  python3.12 tools/ambarella/compare_meeting_llm.py run ... --prompts explicit-owners
  python3.12 tools/ambarella/compare_meeting_llm.py compare DIR OTHER_DIR/qwen35-4b qwen35-4b

tools/ambarella/compare_meeting_llm.sh runs all three and switches the
board's LLM in between.

run      asks what meeting_control_python asks, the way it asks it (flow.py,
         _finish): each topic's summary -- segment_prompt and the topic's
         lines as record.llm_lines_for() writes them -- then the conclusion
         -- meeting_prompt and "第N段：" before each summary -- then
         parse_actions() on the conclusion. The prompts come from the
         extension's own config.py; the lines from a board check's
         record.json, by default the newest in Simplified, which is what the
         graph hands the LLM. An answer is taken as the graph takes it:
         whatever follows </think> if there is one (ambarella_llm2_python's
         ambarella.py), stripped. Every prompt and answer is saved, and
         DIR/<label>/run.json after each answer: a round that stopped goes on
         where it was when it is run again. --prompts NAME asks a set from
         meeting_control_python/prompt_variants.json instead of the
         shipped one; the prompts asked are kept in run.json.
compare  the two rounds side by side. Per topic: seconds, characters, the
         share copied from the transcript, people the meeting does not have
         (titles, owners, numbered placeholders), speaker numbers beyond the
         meeting's, repeated lines, U+FFFD. For the conclusion the same, and
         whether parse_actions() found action items. Whether both rounds were
         asked the same prompts is checked, not assumed. Prints the table and
         writes DIR/compare.html with the answers next to each other. A round
         may also be named by its folder, from another comparison: the same
         model under two prompt sets, say; the page is then
         DIR/compare-<one>-vs-<other>.html.

Run it under python3.12, as check_meeting_board.py: the prompts come from
meeting_control_python's config.py, which needs pydantic.
"""

import argparse
import collections
import glob
import hashlib
import html
import http.client
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ACTIONS = os.path.join(
    REPO,
    "ai_agents",
    "agents",
    "ten_packages",
    "extension",
    "meeting_control_python",
    "actions.py",
)
VARIANTS = os.path.join(
    REPO,
    "ai_agents",
    "agents",
    "ten_packages",
    "extension",
    "meeting_control_python",
    "prompt_variants.json",
)
SHIPPED = "shipped"
THINK_END = "</think>"
# The graph's llm node gives up on a turn after this long (llm
# total_timeout_s); an answer that takes longer is lost there.
GIVE_UP_S = 175.0
# Characters written differently in the two scripts, to tell a record's.
SCRIPT_PAIRS = (
    ("说", "說"),
    ("这", "這"),
    ("们", "們"),
    ("会", "會"),
    ("个", "個"),
    ("为", "為"),
    ("来", "來"),
    ("对", "對"),
    ("时", "時"),
    ("从", "從"),
    ("还", "還"),
    ("与", "與"),
    ("门", "門"),
    ("应", "應"),
    ("过", "過"),
    ("问", "問"),
    ("题", "題"),
    ("点", "點"),
    ("没", "沒"),
    ("话", "話"),
)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The daemon client, the prompts' source and the checks, as the other probe
# has them: one way to ask the 7B, not two.
PMS = load(
    "probe_meeting_summary", os.path.join(HERE, "probe_meeting_summary.py")
)


def say(msg):
    print(f"\n== {msg}", flush=True)


def info(msg):
    print(f"        {msg}", flush=True)


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def write_json(path, value):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def as_graph_takes_it(text):
    """ambarella_llm2_python's reasoning cut, then flow.py's strip."""
    _, delimiter, answer = text.partition(THINK_END)
    return (answer if delimiter else text).strip()


# --- the record ---------------------------------------------------------


def script_of(record):
    text = "".join(
        u.get("text", "")
        for t in record["topics"]
        for u in t.get("utterances") or []
    )
    simplified = sum(text.count(s) for s, _ in SCRIPT_PAIRS)
    traditional = sum(text.count(t) for _, t in SCRIPT_PAIRS)
    return "traditional" if traditional > simplified else "simplified"


def pick_record(path):
    if path:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        if script_of(record) != "simplified":
            info(
                f"{path} is in Traditional; the graph hands the LLM Simplified"
            )
        return path, record
    found = sorted(
        glob.glob(
            os.path.expanduser("~/meeting_probe/board_check/*/record.json")
        )
    )
    for candidate in reversed(found):
        try:
            with open(candidate, encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError):
            continue
        if record.get("topics") and script_of(record) == "simplified":
            return candidate, record
    sys.exit(
        "no board check record.json in Simplified under "
        "~/meeting_probe/board_check: run verify_meeting_board.sh, or pass --record"
    )


def topic_lines(topic):
    """record.py's llm_lines_for(): who said what, no clock."""
    return "\n".join(
        f"说话人{u['speaker'] + 1}：{u['text']}" for u in topic["utterances"]
    )


def speaker_count(record):
    numbers = {
        u["speaker"]
        for t in record["topics"]
        for u in t.get("utterances") or []
    }
    return len(numbers)


def chosen(spec, count):
    """--topics 1-3,5 -> {1, 2, 3, 5}; empty -> every topic."""
    if not spec:
        return set(range(1, count + 1))
    out = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return {n for n in out if 1 <= n <= count}


# --- asking -------------------------------------------------------------


class Daemon:
    """The vendor's daemon, as ambarella_llm2_python asks it: one session
    for the whole round, cleared each turn (Reset-En)."""

    name = "DeepSeek 7B daemon"

    def __init__(self):
        self.url = PMS.setting(
            "AMBARELLA_LLM_BASE_URL", "http://127.0.0.1:8080"
        )
        self.session = PMS.new_session_id()

    def describe(self):
        return {
            "backend": "ambarella daemon",
            "url": self.url,
            "model": "model_type 9",
        }

    def ask(self, prompt, max_seconds, raw_path):
        reply = PMS.ask_patiently(
            self.url, prompt, max_seconds, raw_path, self.session
        )
        # The daemon holds a finished session briefly; do not crowd it.
        time.sleep(5)
        return {
            "raw": reply.mended,
            "first_s": reply.first,
            "total_s": reply.total,
            "ended": reply.ended,
            "finished": reply.ended in PMS.DONE,
            "tokens": None,
        }


class OpenAI:
    """cooper-vllm's OpenAI API, streamed, thinking off, greedy -- the daemon
    decodes greedily too, so one answer per prompt is the model's answer."""

    def __init__(self, url, model, max_tokens):
        self.url = url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.name = f"{model} at {self.url}"

    def describe(self):
        return {
            "backend": "openai",
            "url": self.url,
            "model": self.model,
            "max_tokens": self.max_tokens,
        }

    def ask(self, prompt, max_seconds, raw_path):
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        url = urllib.parse.urlparse(self.url + "/chat/completions")
        conn = http.client.HTTPConnection(
            url.hostname, url.port or 80, timeout=120
        )
        t0 = time.monotonic()
        conn.request(
            "POST",
            url.path,
            json.dumps(body).encode(),
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer EMPTY",
            },
        )
        resp = conn.getresponse()
        raw, buf = bytearray(), b""
        content, reasoning = [], []
        first, ended, tokens = None, "connection closed", None
        if resp.status != 200:
            raw += resp.read()
            ended = f"HTTP {resp.status}: {bytes(raw)[:200].decode('utf-8', 'replace')}"
        while resp.status == 200:
            if time.monotonic() - t0 > max_seconds:
                ended = f"stopped by this probe at {max_seconds:.0f} s"
                break
            chunk = resp.read1(4096)
            if not chunk:
                break
            raw += chunk
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    continue
                event = json.loads(data)
                if event.get("usage"):
                    tokens = event["usage"].get("completion_tokens")
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    for key, into in (
                        ("content", content),
                        ("reasoning_content", reasoning),
                    ):
                        if delta.get(key):
                            into.append(delta[key])
                            if first is None:
                                first = time.monotonic() - t0
                    if choice.get("finish_reason"):
                        ended = choice["finish_reason"]
        conn.close()
        with open(raw_path, "wb") as f:
            f.write(raw)
        text = "".join(content)
        if reasoning:
            # Thinking was asked off; if some came, keep it visible as the
            # daemon's would be, so the graph's cut applies alike.
            text = "".join(reasoning) + THINK_END + text
        return {
            "raw": text,
            "first_s": first,
            "total_s": time.monotonic() - t0,
            "ended": ended,
            "finished": ended == "stop",
            "tokens": tokens,
        }


# --- run ----------------------------------------------------------------


def prompts_for(name):
    """(segment_prompt, meeting_prompt): the shipped ones from the
    extension's config.py, or a set from
    meeting_control_python/prompt_variants.json."""
    if name == SHIPPED:
        control = PMS.control_config()
        return control.segment_prompt, control.meeting_prompt
    with open(VARIANTS, encoding="utf-8") as f:
        sets = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    if name not in sets:
        sys.exit(
            f"no prompt set {name!r}; there are: {SHIPPED}, " + ", ".join(sets)
        )
    return sets[name]["segment_prompt"], sets[name]["meeting_prompt"]


def run(args):
    record_path, record = pick_record(args.record)
    segment_prompt, meeting_prompt = prompts_for(args.prompts)
    topics = record["topics"]
    wanted = chosen(args.topics, len(topics))
    llm = (
        OpenAI(args.openai, args.model, args.max_tokens)
        if args.openai
        else Daemon()
    )
    out = os.path.join(os.path.expanduser(args.out), args.label)
    os.makedirs(out, exist_ok=True)
    state_path = os.path.join(out, "run.json")
    with open(record_path, "rb") as f:
        record_sha = hashlib.sha1(f.read()).hexdigest()[:12]
    head = {
        "label": args.label,
        "llm": llm.describe(),
        "record": record_path,
        "record_sha": record_sha,
        "speakers": speaker_count(record),
        "topics_asked": sorted(wanted),
        "prompts": args.prompts,
        "segment_prompt": segment_prompt,
        "meeting_prompt": meeting_prompt,
        "segment_prompt_sha": sha1(segment_prompt),
        "meeting_prompt_sha": sha1(meeting_prompt),
    }
    earlier = {}
    if os.path.isfile(state_path):
        with open(state_path, encoding="utf-8") as f:
            before = json.load(f)
        same = all(
            before.get(k) == head[k]
            for k in ("record_sha", "segment_prompt_sha", "meeting_prompt_sha")
        )
        if same:
            earlier = {(i["kind"], i["n"]): i for i in before.get("items", [])}
        else:
            info(
                "an earlier round here used another record or other prompts; starting over"
            )
    state = dict(head, started=time.strftime("%F %T"), items=[])

    say(f"{args.label}: {llm.name}")
    info(
        f"record {record_path} ({len(topics)} topics, {head['speakers']} speakers); "
        f"asking topics {args.topics or 'all'}; prompts: {args.prompts}"
    )
    if isinstance(llm, Daemon):
        PMS.thinking()

    def keep(item):
        state["items"].append(item)
        write_json(state_path, state)

    def ask(kind, n, prompt, extra):
        key = (kind, n)
        what = f"topic {n}" if kind == "topic" else "conclusion"
        old = earlier.get(key)
        if old and old.get("prompt_sha") == sha1(prompt) and "answer" in old:
            info(f"{what}: answered in an earlier run, kept")
            keep(old)
            return old["answer"]
        name = f"{kind}-{n:02d}"
        with open(
            os.path.join(out, name + ".prompt.txt"), "w", encoding="utf-8"
        ) as f:
            f.write(prompt)
        got = llm.ask(
            prompt, args.max_seconds, os.path.join(out, name + ".raw")
        )
        answer = as_graph_takes_it(got["raw"])
        with open(
            os.path.join(out, name + ".answer.txt"), "w", encoding="utf-8"
        ) as f:
            f.write(answer)
        first = (
            f"{got['first_s']:.1f} s" if got["first_s"] is not None else "none"
        )
        info(
            f"{what}: {got['total_s']:.1f} s (first character {first}), "
            f"{len(answer)} characters, {'finished' if got['finished'] else got['ended']}"
        )
        info(
            "  "
            + answer[:90].replace("\n", " ")
            + ("…" if len(answer) > 90 else "")
        )
        keep(
            dict(
                extra,
                kind=kind,
                n=n,
                prompt=prompt,
                prompt_sha=sha1(prompt),
                answer=answer,
                **{
                    k: got[k]
                    for k in (
                        "raw",
                        "first_s",
                        "total_s",
                        "ended",
                        "finished",
                        "tokens",
                    )
                },
            )
        )
        return answer

    summaries = {}
    for n, topic in enumerate(topics, 1):
        if n not in wanted or topic.get("error") or not topic.get("utterances"):
            continue
        prompt = segment_prompt + topic_lines(topic)
        summaries[n] = ask(
            "topic",
            n,
            prompt,
            {
                "start_s": topic["start_s"],
                "end_s": topic["end_s"],
                "transcript": "\n".join(u["text"] for u in topic["utterances"]),
                "lines": topic_lines(topic),
            },
        )

    # flow.py numbers by the topic's place in the meeting and leaves out the
    # ones with no summary.
    body = "\n\n".join(
        f"第{n}段：{s}" for n, s in sorted(summaries.items()) if s
    )
    if body:
        conclusion = ask("conclusion", 0, meeting_prompt + body, {})
        parse = load("actions", ACTIONS).parse_actions
        actions, error = parse(conclusion)
        state["actions"] = actions
        state["actions_error"] = error
        info(
            f"parse_actions: {len(actions)} action item(s)"
            + (f"; {error}" if error else "")
        )
    state["ended"] = time.strftime("%F %T")
    write_json(state_path, state)
    print(f"\nsaved in {out}", flush=True)


# --- compare ------------------------------------------------------------


SPEAKER = re.compile(r"[说說][话話]人\s*(\d+)")
MARKDOWN = (
    re.compile(r"(?m)^\s{0,3}#{1,6}\s"),  # headings
    re.compile(r"\*\*[^*\n]+\*\*"),  # bold
    re.compile(r"(?m)^\s*[*\-+]\s+"),  # bullets
)
LIST_ITEM = re.compile(r"(?m)^\s*(?:\d+[.、)）]|[*\-+])\s*")
SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]")


# What a role takes after it in a summary (厨师团队, 保安人员): a role the
# meeting has is not made up.
ROLE_SUFFIXES = ("团队", "人员", "们", "部门", "方面", "这边", "那边")
# A stretch between punctuation, four characters or more.
PHRASE = re.compile(r"[^，、。；：！？,.;:!?\s]{4,}")


def mentioned(text):
    return {int(n) for n in SPEAKER.findall(text)}


def invented_people(answer, transcript):
    """people_named(), less what the transcript itself has: a role someone
    in the meeting gave as theirs (厨师, 保安) is not invented."""
    out = []
    for name in PMS.people_named(answer):
        core = name
        for suffix in ROLE_SUFFIXES:
            if core.endswith(suffix) and len(core) > len(suffix):
                core = core[: -len(suffix)]
        if name not in transcript and core not in transcript:
            out.append(name)
    return out


def looping(answer):
    """How often a phrase comes back after its first two. A model in a loop
    says the same few things again and again within one line, which
    counting repeated lines does not see. Speaker labels do not count."""
    counts = collections.Counter(
        p for p in PHRASE.findall(answer) if not SPEAKER.fullmatch(p)
    )
    return sum(n - 2 for n in counts.values() if n >= 3)


def spoke(lines):
    """The speakers of a topic, from its lines as llm_lines_for() writes them."""
    return {int(n) for n in re.findall(r"(?m)^说话人(\d+)：", lines)}


def measures(item, speakers, summaries_mention, meeting_text):
    """What compare reports for one answer. A topic's summary is held to
    its own transcript: a speaker it names who said nothing in that topic
    was put there by the model. The conclusion sees only summaries, so a
    speaker no summary of its round named was put there by the model too."""
    answer = item["answer"]
    numbers = mentioned(answer)
    _, repeated = PMS.repetition(answer)
    if item["kind"] == "topic":
        silent = numbers - spoke(item.get("lines") or "")
        sentences = len(SENTENCE.findall(answer))
        listed = len(LIST_ITEM.findall(answer)) >= 2
    else:
        silent = numbers - summaries_mention
        sentences, listed = None, False
    return {
        "seconds": item["total_s"],
        "first": item.get("first_s"),
        "chars": len(answer),
        "copied": (
            PMS.copied_share(answer, item["transcript"])
            if item.get("transcript")
            else None
        ),
        "people": invented_people(
            answer, item.get("transcript") or meeting_text
        ),
        "no_such_speaker": sorted(n for n in numbers if n > speakers),
        "silent": sorted(n for n in silent if n <= speakers),
        "markdown": sum(len(r.findall(answer)) for r in MARKDOWN),
        "sentences": sentences,
        "listed": listed,
        "off_length": sentences is not None
        and (listed or not 3 <= sentences <= 5),
        "repeats": sum(repeated.values()) + looping(answer),
        "fffd": answer.count("\ufffd"),
        "finished": item["finished"],
        "ended": item["ended"],
        "lost": item["total_s"] > GIVE_UP_S,
        "tokens": item.get("tokens"),
        "think": THINK_END in (item.get("raw") or ""),
    }


def round_path(root, name):
    """A round: a label in root, or a round's own folder from elsewhere."""
    path = os.path.expanduser(name)
    if os.sep not in name and not os.path.isdir(path):
        path = os.path.join(root, name)
    return path if path.endswith(".json") else os.path.join(path, "run.json")


def compare(args):
    root = os.path.expanduser(args.dir)
    rounds = []
    for name in args.labels or sorted(
        os.path.basename(os.path.dirname(p))
        for p in glob.glob(os.path.join(root, "*", "run.json"))
    ):
        path = round_path(root, name)
        with open(path, encoding="utf-8") as f:
            r = json.load(f)
        r.setdefault("prompts", SHIPPED)  # rounds from before prompt sets
        if r["prompts"] == SHIPPED and not r.get("segment_prompt"):
            try:  # the text as shipped now; needs the extension's pydantic
                r["segment_prompt"], r["meeting_prompt"] = prompts_for(SHIPPED)
            except Exception:  # pylint: disable=broad-except
                pass
        r["_folder"] = os.path.basename(os.path.dirname(os.path.dirname(path)))
        rounds.append(r)
    if len(rounds) != 2:
        sys.exit(
            f"{root} holds {len(rounds)} rounds; compare takes two (give the labels)"
        )
    a, b = rounds[0], rounds[1]
    # What each round is called here: its label, then its prompt set and its
    # comparison's folder as far as it takes to tell the two apart.
    for r in rounds:
        r["_name"] = r["label"]
    if a["prompts"] != b["prompts"]:
        for r in rounds:
            r["_name"] += f" ({r['prompts']})"
    if a["_name"] == b["_name"]:
        for r in rounds:
            r["_name"] += f" [{r['_folder']}]"
    say(f"{a['_name']}  vs  {b['_name']}")
    problems = []
    if a.get("record_sha") != b.get("record_sha"):
        problems.append(
            f"different records: {a.get('record')} / {b.get('record')}"
        )
    if a["prompts"] != b["prompts"]:
        info(
            f"asked different prompt sets, on purpose: {a['prompts']} / {b['prompts']}"
        )
    else:
        for key in ("segment_prompt_sha", "meeting_prompt_sha"):
            if a.get(key) != b.get(key):
                problems.append(f"{key} differs: {a.get(key)} / {b.get(key)}")
    items = {}
    for r in rounds:
        for item in r["items"]:
            items.setdefault((item["kind"], item["n"]), {})[r["_name"]] = item
    if a["prompts"] == b["prompts"]:
        for (kind, n), pair in sorted(items.items()):
            if kind == "topic" and len(pair) == 2:
                sha_a, sha_b = (pair[r["_name"]]["prompt_sha"] for r in rounds)
                if sha_a != sha_b:
                    problems.append(
                        f"topic {n} was asked differently: {sha_a} / {sha_b}"
                    )
    for p in problems:
        info(f"NOT THE SAME QUESTIONS: {p}")
    if not problems:
        info(
            f"the same record ({a['record_sha']})"
            + (" and the same prompts" if a["prompts"] == b["prompts"] else "")
            + " in both rounds"
        )

    speakers = a["speakers"]
    # Per round: every speaker its topic summaries name.
    named = {
        r["_name"]: set().union(
            *(
                mentioned(i["answer"])
                for i in r["items"]
                if i["kind"] == "topic"
            )
        )
        for r in rounds
    }
    # Per round: the whole meeting's transcript, for its conclusion.
    heard = {
        r["_name"]: "\n".join(
            i.get("transcript") or ""
            for i in r["items"]
            if i["kind"] == "topic"
        )
        for r in rounds
    }
    rows = []
    for (kind, n), pair in sorted(
        items.items(), key=lambda kv: (kv[0][0] != "topic", kv[0][1])
    ):
        rows.append(
            (
                (kind, n),
                {
                    lab: measures(it, speakers, named[lab], heard[lab])
                    for lab, it in pair.items()
                },
                pair,
            )
        )

    labels = [a["_name"], b["_name"]]
    width = 60
    print(
        f"\n  {'':<12}"
        + "  ".join(f"{lab[:width]:^{width}}" for lab in labels),
        flush=True,
    )
    heads = (
        f"{'secs':>6}{'chars':>6}{'copied':>7}{'people':>7}{'spk?':>5}"
        f"{'silent':>7}{'md':>4}{'sent':>6}{'rep':>4}{'FFFD':>5}    "
    )
    print(f"  {'':<12}" + "  ".join(heads for _ in labels), flush=True)
    for (kind, n), ms, _ in rows:
        name = f"topic {n}" if kind == "topic" else "conclusion"
        cells = []
        for lab in labels:
            m = ms.get(lab)
            if not m:
                cells.append(f"{'-':>{width}}")
                continue
            copied = f"{m['copied']:.0%}" if m["copied"] is not None else "-"
            mark = "!" if m["lost"] or not m["finished"] else " "
            sent = (
                "-"
                if m["sentences"] is None
                else ("list" if m["listed"] else str(m["sentences"]))
                + ("!" if m["off_length"] else "")
            )
            cells.append(
                f"{m['seconds']:>5.0f}{mark}{m['chars']:>6}{copied:>7}"
                f"{len(m['people']):>7}{len(m['no_such_speaker']):>5}"
                f"{len(m['silent']):>7}{m['markdown']:>4}{sent:>6}"
                f"{m['repeats']:>4}{m['fffd']:>5}    "
            )
        print(f"  {name:<12}" + "  ".join(cells), flush=True)
    for (kind, n), ms, _ in rows:
        for lab in labels:
            m = ms.get(lab)
            if m and m["silent"]:
                where = (
                    f"topic {n}, who said nothing in it"
                    if kind == "topic"
                    else "the conclusion, whom no summary of its round named"
                )
                info(
                    f"{lab} names 说话人{', '.join(map(str, m['silent']))} in {where}"
                )
    for r in rounds:
        total = sum(it["total_s"] for it in r["items"])
        lost = [it for it in r["items"] if it["total_s"] > GIVE_UP_S]
        unfinished = [it for it in r["items"] if not it["finished"]]
        info(
            f"{r['_name']}: {total / 60:.1f} minutes in all; "
            f"{len(lost)} over the graph's {GIVE_UP_S:.0f} s; "
            f"{len(unfinished)} unfinished; parse_actions: "
            f"{len(r.get('actions') or [])} item(s)"
            + (f" ({r['actions_error']})" if r.get("actions_error") else "")
        )
    info("! after the seconds: over the graph's limit, or not finished")
    info(
        "people: titles, owners or numbered placeholders the meeting does not "
        f"have; spk?: speaker numbers beyond its {speakers}; silent: speakers "
        "named who said nothing in that topic (for the conclusion: whom no "
        "summary named); md: Markdown marks; sent: sentences in a summary "
        "(asked for 3-5; ! when not, or a list); rep: repeated lines, and "
        "phrases said a third time or more"
    )

    if args.labels and any(os.sep in n for n in args.labels):
        safe = [
            re.sub(r"[^A-Za-z0-9._-]+", "_", r["_name"]).strip("_")
            for r in rounds
        ]
        page = os.path.join(root, f"compare-{safe[0]}-vs-{safe[1]}.html")
    else:
        page = os.path.join(root, "compare.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(render(rounds, rows, problems, speakers))
    print(f"\nside by side: {page}", flush=True)


def render(rounds, rows, problems, speakers):
    labels = [r["_name"] for r in rounds]
    e = html.escape
    columns = 10

    def cell(m):
        if not m:
            return "<td>-</td>" * columns
        copied = f"{m['copied']:.0%}" if m["copied"] is not None else "-"
        late = ' class="bad"' if m["lost"] or not m["finished"] else ""

        def flagged(value, bad):
            return (
                f'<td class="bad">{value}</td>' if bad else f"<td>{value}</td>"
            )

        if m["sentences"] is None:
            sent = "<td>-</td>"
        else:
            sent = flagged(
                "條列" if m["listed"] else m["sentences"], m["off_length"]
            )
        return (
            f"<td{late}>{m['seconds']:.0f}</td><td>{m['chars']}</td><td>{copied}</td>"
            + flagged(e("、".join(m["people"])) or "-", m["people"])
            + flagged(
                e(", ".join(map(str, m["no_such_speaker"]))) or "-",
                m["no_such_speaker"],
            )
            + flagged(e(", ".join(map(str, m["silent"]))) or "-", m["silent"])
            + flagged(m["markdown"], m["markdown"])
            + sent
            + flagged(m["repeats"], m["repeats"])
            + flagged(m["fffd"], m["fffd"])
        )

    def marked(answer, silent):
        """The answer, with the speakers who should not be in it marked."""
        out = e(answer)
        if silent:
            out = SPEAKER.sub(
                lambda mo: (
                    f"<mark>{mo.group(0)}</mark>"
                    if int(mo.group(1)) in silent
                    else mo.group(0)
                ),
                out,
            )
        return out

    head = "".join(f'<th colspan="{columns}">{e(lab)}</th>' for lab in labels)
    sub = (
        "<th>秒</th><th>字</th><th>抄寫</th><th>捏造的人</th><th>不存在的說話人</th>"
        "<th>沒發言卻被提到</th><th>Markdown</th><th>句數</th><th>重複</th><th>亂碼</th>"
    ) * len(labels)
    table = []
    for (kind, n), ms, _ in rows:
        name = f"話題 {n}" if kind == "topic" else "結論"
        table.append(
            f"<tr><th>{name}</th>"
            + "".join(cell(ms.get(lab)) for lab in labels)
            + "</tr>"
        )
    sections = []
    for (kind, n), ms, pair in rows:
        any_item = next(iter(pair.values()))
        if kind == "topic":
            lines = any_item["lines"]
            who = "、".join(f"说话人{s}" for s in sorted(spoke(lines)))
            title = f"話題 {n}（{PMS.mmss(any_item['start_s'])}–{PMS.mmss(any_item['end_s'])}）"
            context = (
                f'<p class="who">這段發言的人：{e(who)}</p>'
                f"<details><summary>完整逐字稿（{len(lines.splitlines())} 行）</summary>"
                f"<pre>{e(lines)}</pre></details>"
            )
        else:
            title, context = "會議結論", ""
        answers = "".join(
            f'<div class="answer"><h4>{e(lab)}'
            + (
                f"<span>{ms[lab]['seconds']:.0f} 秒 · {ms[lab]['chars']} 字</span>"
                if lab in ms
                else ""
            )
            + "</h4><pre>"
            + (
                marked(pair[lab]["answer"], set(ms[lab]["silent"]))
                if lab in pair
                else "（沒有回答）"
            )
            + "</pre></div>"
            for lab in labels
        )
        sections.append(
            f'<section><h3>{title}</h3>{context}<div class="pair">{answers}</div></section>'
        )
    actions = "".join(
        f"<div class=\"answer\"><h4>{e(r['_name'])}：parse_actions</h4><pre>"
        + e(json.dumps(r.get("actions") or [], ensure_ascii=False, indent=1))
        + (f"\n\n{e(r['actions_error'])}" if r.get("actions_error") else "")
        + "</pre></div>"
        for r in rounds
    )
    warn = "".join(f'<p class="bad">提示詞不同：{e(p)}</p>' for p in problems)
    asked = "".join(
        f"<details><summary>{e(r['_name'])} 用的提示詞（{e(r['prompts'])}）</summary>"
        f"<pre>摘要：{e(r.get('segment_prompt') or '（這一輪沒有存下）')}"
        f"結論：{e(r.get('meeting_prompt') or '（這一輪沒有存下）')}</pre></details>"
        for r in rounds
    )
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>會議紀錄 LLM 比較</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1b1f24; background: #fff; }}
 table {{ border-collapse: collapse; font-size: 14px; }}
 th, td {{ border: 1px solid #d0d7de; padding: 4px 8px; text-align: right; }}
 th {{ background: #f6f8fa; }}
 .bad {{ color: #b42318; font-weight: 600; }}
 mark {{ background: #fde4df; color: #b42318; font-weight: 600; }}
 section {{ margin-top: 28px; }}
 .who {{ color: #57606a; margin: 4px 0; }}
 .pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
 .answer {{ border: 1px solid #d0d7de; border-radius: 8px; padding: 8px 12px; }}
 .answer h4 {{ margin: 4px 0 8px; display: flex; justify-content: space-between; }}
 .answer h4 span {{ font-weight: 400; color: #57606a; }}
 pre {{ white-space: pre-wrap; font-family: inherit; line-height: 1.6; margin: 0; }}
 details pre {{ color: #57606a; font-size: 14px; }}
</style></head><body>
<h1>會議紀錄 LLM 比較：{e(' vs '.join(labels))}</h1>
<p>同一份紀錄（{e(rounds[0]['record'])}，{speakers} 位說話人）；提示詞：{e(' / '.join(r['prompts'] for r in rounds))}。
「抄寫」是回答中與逐字稿逐字相同（連續 10 字以上）的比例；「捏造的人」是會議裡沒有的稱謂、負責人或編號佔位（逐字稿裡有的角色，如廚師、保安，不算）；「重複」是重複的行，加上同一片段第三次以後的出現次數；
「沒發言卻被提到」是摘要提到、但在那段逐字稿裡一句話都沒說的說話人（結論則是各段摘要都沒提過的說話人），在回答裡標紅；
「Markdown」是標題、粗體、項目符號的數量，App 與 minutes.txt 會原樣顯示這些符號；
「句數」對照摘要要求的三到五句，寫成條列也標紅；
紅色秒數表示超過 graph 的 {GIVE_UP_S:.0f} 秒上限或沒寫完。</p>
{warn}
{asked}
<table><tr><th></th>{head}</tr><tr><th></th>{sub}</tr>{''.join(table)}</table>
{''.join(sections)}
<section><h3>待辦事項（parse_actions）</h3><div class="pair">{actions}</div></section>
</body></html>
"""


def main():
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="what", required=True)
    r = sub.add_parser("run", help="one LLM answers every prompt")
    r.add_argument("--label", required=True)
    r.add_argument("--out", required=True, help="the comparison's folder")
    r.add_argument(
        "--record",
        help="a record.json (default: newest Simplified board check)",
    )
    r.add_argument("--topics", help="e.g. 1-3 or 1,4,7 (default: all)")
    r.add_argument(
        "--openai",
        help="an OpenAI-style API, e.g. http://127.0.0.1:8000/v1 "
        "(default: the vendor's daemon)",
    )
    r.add_argument("--model", default="qwen3.5_4B")
    r.add_argument("--max-tokens", type=int, default=2048)
    r.add_argument("--max-seconds", type=float, default=600.0)
    r.add_argument(
        "--prompts",
        default=SHIPPED,
        help=f"{SHIPPED} (meeting_control_python's), or a set in "
        "meeting_control_python/prompt_variants.json",
    )
    c = sub.add_parser("compare", help="two rounds side by side")
    c.add_argument("dir", help="the comparison's folder; the page goes here")
    c.add_argument(
        "labels",
        nargs="*",
        help="two rounds: labels in DIR, or round folders from elsewhere "
        "(default: the two rounds in DIR)",
    )
    args = p.parse_args()
    if args.what == "run":
        run(args)
    else:
        compare(args)


if __name__ == "__main__":
    main()
