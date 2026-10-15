#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""meeting_minutes_qwen (examples/voice-assistant/tenapp/property.json) asks
Qwen3.5-4B what the 2026-10-15 evaluation asked it -- the explicit-owners
prompts word for word, no system message, greedy, no repetition penalty --
and is otherwise the meeting_minutes graph, node for node."""

import json
import os

EXT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROPERTY = os.path.join(
    EXT,
    "..",
    "..",
    "..",
    "examples",
    "voice-assistant",
    "tenapp",
    "property.json",
)
VARIANTS = os.path.join(EXT, "prompt_variants.json")
CHANGED = {"segment_prompt", "meeting_prompt", "summary_timeout_s"}


def graph(name):
    with open(PROPERTY, encoding="utf-8") as f:
        data = json.load(f)
    return next(
        g for g in data["ten"]["predefined_graphs"] if g["name"] == name
    )


def nodes(name):
    return {n["name"]: n for n in graph(name)["graph"]["nodes"]}


def test_the_prompts_are_the_explicit_owners_set():
    with open(VARIANTS, encoding="utf-8") as f:
        wanted = json.load(f)["explicit-owners"]
    control = nodes("meeting_minutes_qwen")["main_control"]["property"]
    assert control["segment_prompt"] == wanted["segment_prompt"]
    assert control["meeting_prompt"] == wanted["meeting_prompt"]
    assert control["summary_timeout_s"] == 300.0


def test_the_llm_is_qwen_as_it_was_evaluated():
    llm = nodes("meeting_minutes_qwen")["llm"]
    assert llm["addon"] == "openai_llm2_python"
    assert llm["property"] == {
        "base_url": "${env:QWEN_LLM_BASE_URL|http://127.0.0.1:8000/v1}",
        "api_key": "EMPTY",
        "model": "qwen3.5_4B",
        "prompt": "",
        "temperature": 0.0,
        "frequency_penalty": 0.0,
        "max_tokens": 2048,
    }


def test_otherwise_it_is_the_meeting_minutes_graph():
    old, new = nodes("meeting_minutes"), nodes("meeting_minutes_qwen")
    assert set(old) == set(new)
    for name in old:
        if name == "llm":
            continue
        assert old[name]["addon"] == new[name]["addon"], name
        before = dict(old[name].get("property") or {})
        after = dict(new[name].get("property") or {})
        if name == "main_control":
            for key in CHANGED:
                before.pop(key, None)
                after.pop(key, None)
        assert before == after, name
    assert (
        graph("meeting_minutes")["graph"]["connections"]
        == graph("meeting_minutes_qwen")["graph"]["connections"]
    )
