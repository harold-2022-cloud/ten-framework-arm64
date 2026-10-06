#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Action items, best effort.

Measured on the board over six rounds: the 7B either finds the actions or
keeps a format, never both (PRD Part 2). So the meeting turn asks for prose,
the prose is kept whole as the summary, and this only reads actions out of a
JSON block when the answer happens to contain one. Anything else is an
empty list and a reason -- never an exception, never at the summary's cost.
"""

import json
import re
from typing import List, Optional, Tuple

FIELDS = {
    "what": ("what", "item", "task", "事項", "內容", "待辦"),
    "owner": ("owner", "who", "負責人", "負責"),
    "due_raw": ("due", "due_raw", "when", "deadline", "期限"),
    "raised_at": ("raised_at", "time", "at", "時間"),
}


def parse_actions(answer: str) -> Tuple[List[dict], Optional[str]]:
    block = _first_json_object(answer or "")
    if block is None:
        return [], "no JSON object in the answer"
    try:
        data = json.loads(block)
    except ValueError as err:
        return [], f"JSON block did not parse: {err}"
    items = _first_list_of_dicts(data)
    if items is None:
        return [], "JSON block holds no list of items"
    return [_normalise(item) for item in items], None


def _first_json_object(text: str) -> Optional[str]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]  # unbalanced: let json.loads say what is wrong


def _first_list_of_dicts(data) -> Optional[list]:
    if (
        isinstance(data, list)
        and data
        and all(isinstance(x, dict) for x in data)
    ):
        return data
    if isinstance(data, dict):
        for value in data.values():
            found = _first_list_of_dicts(value)
            if found is not None:
                return found
    return None


def _normalise(item: dict) -> dict:
    out = {}
    for name, aliases in FIELDS.items():
        out[name] = next(
            (str(item[a]) for a in aliases if item.get(a) not in (None, "")),
            None,
        )
    return out
