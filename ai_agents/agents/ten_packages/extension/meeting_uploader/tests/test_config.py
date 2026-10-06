#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The defaults in code are the ones the extension ships with.

In a graph the runtime fills the config from property.json; a test or a
script building MeetingUploaderConfig() gets the defaults in config.py.
meeting_segmenter's two once disagreed, and a run that bypassed the graph
measured something the extension would never do.
"""

import json
from pathlib import Path

from meeting_uploader.config import MeetingUploaderConfig

SHIPPED = json.loads(
    (Path(__file__).parent.parent / "property.json").read_text(encoding="utf-8")
)


def test_every_shipped_property_is_also_the_default_in_code():
    defaults = MeetingUploaderConfig().model_dump()

    assert {k: defaults[k] for k in SHIPPED} == SHIPPED
