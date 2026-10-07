#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The record in the script the user chose: Traditional or Simplified.

Everything the models write is Simplified -- SenseVoice's transcript and
the board LLM's summaries -- while the record's own labels (說話人, 結論)
are Traditional. The prompts stay as they are: Simplified is what the
board's 7B was measured on, and Traditional in its prompt brought more
garbled characters and drift. So the choice is applied once, to the
finished record.json and minutes.txt, and every part of the record ends up
in the same script.

OpenCC, the pure-Python reimplementation: no native code to build on the
board. s2tw converts characters to Taiwan's standard forms without
rewording ("软件" stays 軟件, not 軟體): the record keeps the speakers'
own words.
"""

from typing import Callable, Dict

SCRIPTS = {"traditional": "s2tw", "simplified": "t2s"}

# Values that name things rather than say them, left as they are.
IDENTIFIERS = {"meeting_id", "id", "audio", "error", "actions_error"}

_converters: Dict[str, Callable[[str], str]] = {}


def converter(script: str) -> Callable[[str], str]:
    """A function converting text to the script, built once per script."""
    if script not in SCRIPTS:
        raise ValueError(
            f"script {script!r}: one of {', '.join(sorted(SCRIPTS))}"
        )
    if script not in _converters:
        from opencc import OpenCC  # pylint: disable=import-outside-toplevel

        _converters[script] = OpenCC(SCRIPTS[script]).convert
    return _converters[script]


def convert_record(value, convert: Callable[[str], str]):
    """record.json with every string converted but the identifiers."""
    if isinstance(value, dict):
        return {
            k: v if k in IDENTIFIERS else convert_record(v, convert)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [convert_record(v, convert) for v in value]
    if isinstance(value, str):
        return convert(value)
    return value
