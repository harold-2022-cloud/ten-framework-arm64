#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A stand-in for ten_env, faked to the contract on_init actually uses:
get_property_to_json and the four log methods. Kept local to this
extension -- sherpa_onnx_asr_python has one shaped the same way, but this
one is not imported from there.
"""

import json
from typing import List, Optional


class FakeTenEnv:
    def __init__(self, properties: Optional[dict] = None) -> None:
        self.lines: List[str] = []
        self.properties = properties or {}

    async def get_property_to_json(self, _path: str):
        return json.dumps(self.properties), None

    def _record(self, message: str, **_kwargs) -> None:
        self.lines.append(message)

    log_debug = _record
    log_info = _record
    log_warn = _record
    log_error = _record
