#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The defaults in code are the ones the extension ships with.

In a graph the runtime fills the config from property.json; anything else
-- a script, a test, the probe -- builds MeetingSegmenterConfig() and gets
the defaults in config.py. When the two disagreed on vad_threshold (0.5 in
code, 0.3 shipped), an end-to-end run cut a 38-minute meeting into nine
topics where the extension cuts five: at 0.5 the VAD misses half the speech
of a far-field recording and hears silences that are not there.
"""

import json
from pathlib import Path

from meeting_segmenter.config import MeetingSegmenterConfig

SHIPPED = json.loads(
    (Path(__file__).parent.parent / "property.json").read_text(encoding="utf-8")
)


def test_every_shipped_property_is_also_the_default_in_code():
    defaults = MeetingSegmenterConfig().model_dump()

    assert {k: defaults[k] for k in SHIPPED} == SHIPPED
