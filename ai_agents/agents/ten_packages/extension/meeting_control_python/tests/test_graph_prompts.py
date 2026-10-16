#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""meeting_minutes (examples/voice-assistant/tenapp/property.json) asks
Qwen3.5-4B what the 2026-10-15 evaluation asked it -- the explicit-owners
prompts word for word, no system message, greedy, no repetition penalty --
with TEN's path timeouts past the controller's 300 s turn. It is the only
meeting graph: meeting_minutes_qwen, beside it while it was tried, is gone."""

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
# TEN times a command out on its own path after 180 s by default
# (core/include_internal/ten_runtime/extension/path_timer.h), and a streamed
# answer does not reset it: without these the 300 s turn would end at 180 s.
PATH_TIMEOUT_US = 330_000_000


def graphs():
    with open(PROPERTY, encoding="utf-8") as f:
        return json.load(f)["ten"]["predefined_graphs"]


def nodes(name):
    graph = next(g for g in graphs() if g["name"] == name)
    return {n["name"]: n for n in graph["graph"]["nodes"]}


def test_the_prompts_are_the_explicit_owners_set():
    with open(VARIANTS, encoding="utf-8") as f:
        wanted = json.load(f)["explicit-owners"]
    control = nodes("meeting_minutes")["main_control"]["property"]
    assert control["segment_prompt"] == wanted["segment_prompt"]
    assert control["meeting_prompt"] == wanted["meeting_prompt"]
    assert control["summary_timeout_s"] == 300.0


def test_tens_path_timeouts_outlast_the_turn():
    control = nodes("meeting_minutes")["main_control"]["property"]
    llm = nodes("meeting_minutes")["llm"]["property"]
    assert control["ten"] == {"path_timeout": {"out_path": PATH_TIMEOUT_US}}
    assert llm["ten"] == {"path_timeout": {"in_path": PATH_TIMEOUT_US}}
    assert PATH_TIMEOUT_US > control["summary_timeout_s"] * 1_000_000


def test_the_llm_is_qwen_as_it_was_evaluated():
    llm = nodes("meeting_minutes")["llm"]
    assert llm["addon"] == "openai_llm2_python"
    assert llm["property"] == {
        "base_url": "${env:QWEN_LLM_BASE_URL|http://127.0.0.1:8000/v1}",
        "api_key": "EMPTY",
        "model": "qwen3.5_4B",
        "prompt": "",
        "temperature": 0.0,
        "frequency_penalty": 0.0,
        "max_tokens": 2048,
        "ten": {"path_timeout": {"in_path": PATH_TIMEOUT_US}},
    }


def test_there_is_one_meeting_graph():
    names = [g["name"] for g in graphs()]
    assert names.count("meeting_minutes") == 1
    assert "meeting_minutes_qwen" not in names


def test_the_transcriber_tells_the_controller_how_far_it_got():
    graph = next(g for g in graphs() if g["name"] == "meeting_minutes")
    sent = {
        (c["extension"], d["name"], dest["extension"])
        for c in graph["graph"]["connections"]
        for d in c.get("data", [])
        for dest in d["dest"]
    }
    assert ("transcriber", "transcribe_progress", "main_control") in sent
