#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""work/state.json: how far a meeting has got.

GET /meeting/{id} answers from this file, so it is written whole -- to a
temporary file renamed into place -- and never seen half-written. Each
write keeps what earlier ones said: topics_total stays put while
topics_done climbs.
"""

import json
import os
import time


def write_state(work_dir: str, state: str, **fields) -> dict:
    path = os.path.join(work_dir, "state.json")
    current: dict = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            current = json.load(f)
    current.update(fields)
    current["state"] = state
    current["updated_at"] = time.time()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False)
    os.replace(tmp, path)
    return current
