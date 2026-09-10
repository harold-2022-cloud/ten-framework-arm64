# Ambarella ASR/TTS Extensions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship two TEN extensions that wrap the Ambarella `asr_d` and `tts_d`
resident daemons as ordinary ASR and TTS providers, plus an example graph that
runs a full voice agent on an N1-655 board.

**Architecture:** Each extension owns one child process and talks to it over a
line protocol on stdin/stdout. A dependency-free `daemon.py` per package handles
spawn, the readiness handshake, one-request-one-response, and clean shutdown.
The ASR extension buffers PCM and infers only on `finalize()`; the TTS extension
synthesises to a WAV, reads it back, resamples 22050→16000 and yields PCM.
Because the protocol boundary is a subprocess, a Python stub daemon exercises
every state on x86 CI with no aarch64 hardware.

**Tech Stack:** Python 3.10+, `ten_runtime`, `ten_ai_base` 0.7 (registry
dependency), asyncio subprocesses, stdlib `wave`, `numpy` + `scipy` (TTS only),
pytest with `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **Formatting is CI-blocking.** `black --line-length 80`. Run `task format`
  from `/app` before every commit; `task check` fails the build otherwise.
- **Lint is CI-blocking with zero tolerance.** `task lint-extension
  EXTENSION=<dir>` — *any* pylint warning fails.
- **Import from `ten_runtime`, never `ten`** (the pre-0.11 module name).
- **`ten_env.get_property_*()` returns a `(value, error)` tuple.** Always take
  `[0]`.
- **No signal handlers and no `atexit`** — extensions run off the main thread.
  All cleanup goes in `on_stop()` / `stop_connection()` / `clean()`.
- **Three names must match exactly** or the graph fails to load silently: the
  `@register_addon_as_extension` argument, `manifest.json`'s `name`, and the
  graph node's `addon` field.
- **Extension `version` fields under `ai_agents/` are hand-maintained.** Both
  new packages start at `0.1.0`. (The generated-version rule applies to
  `core/` and `packages/` only.)
- **Never edit generated artifacts:** `out/`, `manifest-lock.json`, `.ten/`,
  `bin/`.
- **A stale `.ten/` breaks the next install.** If `task check` reports phantom
  reformatting, `rm -rf <ext>/.ten`.
- **Everything runs in the container.** `docker exec ten_agent_dev bash -c "cd
  /app && ..."`. The host has none of `task`, `black`, `pylint`, `tman`.
- **Fixed audio facts** (decoded from the binaries, spec §2.3): `asr_d`
  consumes 16000 Hz PCM16 mono; `tts_d` emits 22050 Hz PCM16 mono. Neither is
  configurable. The transport is 16 kHz G.722 both ways, so ASR needs no
  conversion and TTS must resample to 16000.
- **Commits:** conventional-commits, enforced by `commitlint` in CI with no
  local hook. Hard-wrap bodies at 100 characters per line. No `Co-Authored-By`
  trailers and no mention of AI tool names anywhere in commits or branches.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `ambarella_asr_python/const.py` | Fixed protocol and audio constants. No logic. |
| `ambarella_asr_python/config.py` | Pydantic config; maps TEN language codes to daemon words; builds the daemon's argv. Imports nothing from `ten_ai_base`. |
| `ambarella_asr_python/daemon.py` | The line-protocol client: spawn, handshake, request, quit. Stdlib + asyncio only, so its tests run anywhere. |
| `ambarella_asr_python/extension.py` | `AsyncASRBaseExtension` implementation: buffering, the 30 s ceiling, `finalize()`, error classification, restart policy. |
| `ambarella_asr_python/addon.py` | Addon registration. |
| `ambarella_asr_python/tests/stub_daemon.py` | A Python stand-in speaking the same protocol, driven by `--scenario`. The reason no hardware is needed. |
| `ambarella_tts_python/const.py` | Fixed protocol and audio constants. |
| `ambarella_tts_python/config.py` | `AsyncTTS2HttpConfig` subclass; builds the daemon's argv. |
| `ambarella_tts_python/daemon.py` | Same client, `READY tts` token. Duplicated by design (spec §3). |
| `ambarella_tts_python/ambarella_tts.py` | `AsyncTTS2HttpClient`: text sanitising, `INFER`, WAV→PCM, resample, chunked yield, cancel. |
| `ambarella_tts_python/extension.py` | Four-method `AsyncTTS2HttpExtension` shim. |
| `examples/voice-assistant-ambarella/tenapp/property.json` | The graph: nodes and typed connections. |
| `examples/voice-assistant-ambarella/tenapp/manifest.json` | Path dependencies for the three Ambarella extensions. |
| `tools/ambarella/vp_concurrency_probe.py` | The on-board measurement the spec requires before shipping (spec §9). |

`config.py` and `daemon.py` deliberately avoid importing `ten_ai_base` so their
tests run on any machine; only `extension.py` tests need the container.

---

### Task 1: ASR package scaffolding and config

**Files:**
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/__init__.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/const.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/config.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/pyproject.toml`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/requirements.txt`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/tests/__init__.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/tests/bin/start`
- Test: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `AmbarellaASRConfig` with fields `bin_path: str`, `model_dir: str`,
  `model_type: str`, `tmp_dir: str`, `min_audio_ms: int`, `load_timeout_s:
  float`, `infer_timeout_s: float`, `quit_timeout_s: float`,
  `restart_max_attempts: int`, `params: dict[str, Any]`; properties
  `daemon_language -> str` and `normalized_language -> str`; method
  `daemon_flags() -> list[str]`. Constants `SAMPLE_RATE = 16000`,
  `BYTES_PER_SECOND = 32000`, `MAX_BUFFER_BYTES = 960_000`,
  `READY_TOKEN = "READY asr"`, `NO_SPEECH_ERR = "ERR no speech."`,
  `MODULE_NAME_ASR = "asr"`, `LOG_CATEGORY_VENDOR = "vendor"`,
  `LOG_CATEGORY_KEY_POINT = "key_point"`.

- [ ] **Step 1: Create the package skeleton**

```bash
cd /root/ten-framework/ai_agents/agents/ten_packages/extension
mkdir -p ambarella_asr_python/tests/bin
```

`ambarella_asr_python/__init__.py`:

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
from . import addon
```

`ambarella_asr_python/tests/__init__.py`:

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
```

`ambarella_asr_python/tests/bin/start`:

```bash
#!/bin/bash

set -e

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

export PYTHONPATH=.ten/app:.ten/app/ten_packages/system/ten_runtime_python/lib:.ten/app/ten_packages/system/ten_runtime_python/interface:.ten/app/ten_packages/system/ten_ai_base/interface:$PYTHONPATH

pytest -s tests/ "$@"
```

Then `chmod +x ambarella_asr_python/tests/bin/start`.

`ambarella_asr_python/pyproject.toml`:

```toml
[project]
name = "ambarella-asr-python"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []
```

`ambarella_asr_python/requirements.txt` — an empty file. The ASR side needs
only the standard library; write it with `: > ambarella_asr_python/requirements.txt`.

- [ ] **Step 2: Write `const.py`**

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

MODULE_NAME_ASR = "asr"

# ten_ai_base exports these too, but const.py must stay importable without it
# so that config and daemon tests can run outside the container.
LOG_CATEGORY_VENDOR = "vendor"
LOG_CATEGORY_KEY_POINT = "key_point"

# asr_d prints this once the model is resident in VP memory.
READY_TOKEN = "READY asr"

# The daemon's answer to silence. A normal outcome, not an error.
NO_SPEECH_ERR = "ERR no speech."

# 16000 is compiled into asr_d (movz w3, #16000); it is not configurable.
SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2

# asr_d accepts at most 30 seconds of audio per INFER.
MAX_BUFFER_BYTES = BYTES_PER_SECOND * 30
```

- [ ] **Step 3: Write the failing config test**

`ambarella_asr_python/tests/test_config.py`:

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

from ambarella_asr_python.config import AmbarellaASRConfig
from ambarella_asr_python.const import MAX_BUFFER_BYTES


def test_defaults():
    config = AmbarellaASRConfig()
    assert config.model_type == "tiny"
    assert config.tmp_dir == "/tmp"
    assert config.min_audio_ms == 200
    assert config.load_timeout_s == 180.0
    assert config.infer_timeout_s == 30.0
    assert config.quit_timeout_s == 5.0
    assert config.restart_max_attempts == 3
    assert config.params == {}


def test_buffer_ceiling_matches_thirty_seconds():
    assert MAX_BUFFER_BYTES == 960_000


def test_flags_carry_model_dir_and_type():
    config = AmbarellaASRConfig(
        bin_path="/opt/asr_d", model_dir="/models/whisper"
    )
    flags = config.daemon_flags()
    assert flags[:4] == ["--cavalry_dir", "/models/whisper", "--type", "tiny"]


def test_flags_expand_params_deterministically():
    config = AmbarellaASRConfig(
        model_dir="/m",
        params={"beam_size": 5, "no_speech_thres": 0.6, "language": "chinese"},
    )
    flags = config.daemon_flags()
    assert flags == [
        "--cavalry_dir",
        "/m",
        "--type",
        "tiny",
        "--beam_size",
        "5",
        "--language",
        "chinese",
        "--log",
        "1",
        "--no_speech_thres",
        "0.6",
    ]


def test_log_defaults_to_error_level():
    config = AmbarellaASRConfig(model_dir="/m")
    flags = config.daemon_flags()
    assert flags[flags.index("--log") + 1] == "1"


def test_explicit_log_level_wins():
    config = AmbarellaASRConfig(model_dir="/m", params={"log": 4})
    flags = config.daemon_flags()
    assert flags[flags.index("--log") + 1] == "4"


def test_cap_dev_is_dropped_so_infer_mic_stays_unavailable():
    config = AmbarellaASRConfig(model_dir="/m", params={"cap_dev": "default"})
    assert "--cap_dev" not in config.daemon_flags()


def test_ten_language_codes_map_to_daemon_words():
    for code in ("zh", "zh-CN", "zh-TW"):
        assert AmbarellaASRConfig(params={"language": code}).daemon_language == (
            "chinese"
        )
    for code in ("en", "en-US", "en-GB"):
        assert AmbarellaASRConfig(params={"language": code}).daemon_language == (
            "english"
        )


def test_auto_language_passes_through():
    config = AmbarellaASRConfig(params={"language": "auto"})
    assert config.daemon_language == "auto"


def test_language_defaults_to_chinese():
    assert AmbarellaASRConfig().daemon_language == "chinese"


def test_normalized_language_reports_ten_codes():
    assert AmbarellaASRConfig(params={"language": "zh"}).normalized_language == (
        "zh-CN"
    )
    assert AmbarellaASRConfig(
        params={"language": "english"}
    ).normalized_language == "en-US"
```

- [ ] **Step 4: Run the test to verify it fails**

```bash
docker exec ten_agent_dev bash -c "cd /app/agents/ten_packages/extension && python3 -m pytest ambarella_asr_python/tests/test_config.py -v"
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ambarella_asr_python.config'`.

- [ ] **Step 5: Write `config.py`**

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

from typing import Any, Dict, List

from pydantic import BaseModel, Field

# asr_d wants whole words, not ISO codes: --language chinese, not --language zh.
_DAEMON_LANGUAGE = {
    "zh": "chinese",
    "zh-CN": "chinese",
    "zh-TW": "chinese",
    "en": "english",
    "en-US": "english",
    "en-GB": "english",
}

# Reported back on ASRResult, mirroring whisper_stt_python.normalized_language.
_TEN_LANGUAGE = {
    "chinese": "zh-CN",
    "english": "en-US",
}

# --cap_dev would enable INFER_MIC, which bypasses the TEN audio pipeline.
# This extension is file-mode only, so the flag is dropped rather than honoured.
_DROPPED_PARAMS = ("cap_dev",)


class AmbarellaASRConfig(BaseModel):
    """Configuration for the on-board asr_d daemon."""

    bin_path: str = ""
    model_dir: str = ""
    model_type: str = "tiny"
    tmp_dir: str = "/tmp"

    # Audio shorter than this never reaches the VP; asr_d answers
    # ERR audio_too_short and the round trip is wasted. The daemon's real
    # file-mode floor is undocumented -- see spec section 9.
    min_audio_ms: int = 200

    load_timeout_s: float = 180.0
    infer_timeout_s: float = 30.0
    quit_timeout_s: float = 5.0

    # Every restart pays a full model load, so the retry loop is capped.
    restart_max_attempts: int = 3

    params: Dict[str, Any] = Field(default_factory=dict)

    @property
    def daemon_language(self) -> str:
        """The --language value asr_d expects."""
        raw = str(self.params.get("language", "chinese"))
        return _DAEMON_LANGUAGE.get(raw, raw)

    @property
    def normalized_language(self) -> str:
        """The language code reported back to the pipeline."""
        return _TEN_LANGUAGE.get(self.daemon_language, self.daemon_language)

    def daemon_flags(self) -> List[str]:
        """Build asr_d's argv from model_dir plus the params pass-through."""
        flags = [
            "--cavalry_dir",
            self.model_dir,
            "--type",
            self.model_type,
        ]
        params = {
            key: value
            for key, value in self.params.items()
            if key not in _DROPPED_PARAMS
        }
        params["language"] = self.daemon_language
        params.setdefault("log", 1)
        for key in sorted(params):
            flags.extend([f"--{key}", str(params[key])])
        return flags
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
docker exec ten_agent_dev bash -c "cd /app/agents/ten_packages/extension && python3 -m pytest ambarella_asr_python/tests/test_config.py -v"
```

Expected: PASS, 11 tests.

- [ ] **Step 7: Format and commit**

```bash
docker exec ten_agent_dev bash -c "cd /app && task format"
cd /root/ten-framework
git add ai_agents/agents/ten_packages/extension/ambarella_asr_python
git commit -m "feat: add ambarella asr package scaffolding and config"
```

---

### Task 2: The line-protocol daemon client

**Files:**
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/daemon.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/tests/stub_daemon.py`
- Test: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/tests/test_daemon.py`

**Interfaces:**
- Consumes: `READY_TOKEN`, `LOG_CATEGORY_VENDOR` from `const.py` (Task 1).
- Produces: `DaemonError(RuntimeError)`; `DaemonClient(bin_path: str, flags:
  list[str], ready_token: str, logger, load_timeout_s: float = 180.0,
  quit_timeout_s: float = 5.0, log_category: str = "vendor")` with
  `async start() -> str`, `async wait_ready(timeout: float) -> None`,
  `async request(command: str, timeout: float) -> str` (returns the terminal
  `OK …`/`ERR …` line — an `ERR` line is a *return value*, not an exception),
  `async stop() -> None`, and properties `alive: bool` / `ready: bool`.

- [ ] **Step 1: Write the stub daemon**

`ambarella_asr_python/tests/stub_daemon.py`:

```python
#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A stand-in for asr_d / tts_d that speaks the same line protocol.

Driven by --scenario. Every other flag is ignored, so the real argv the
extension builds can be passed through unchanged; tests select a scenario by
adding it to the extension's `params`, which the pass-through expands into
--scenario <name>.
"""

import argparse
import struct
import sys
import time
import wave


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit_noise() -> None:
    """Imitate EazyAI's own Notice-level chatter on the same stdout."""
    emit("[INFO] [01-01 17:33:55] Device ENABLE: fd_dev: 5, net_type: 9")
    emit("[NOTICE] cavalry: vp memory 41f00000 reserved")


def write_wav(path: str, rate: int, seconds: float) -> int:
    frames = int(rate * seconds)
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(struct.pack("<%dh" % frames, *([0] * frames)))
    return frames


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="ok")
    parser.add_argument("--ready-token", default="READY asr")
    parser.add_argument("--stub-text", default="hello world")
    parser.add_argument("--stub-rate", type=int, default=22050)
    parser.add_argument("--stub-delay", type=float, default=0.0)
    args, _ignored = parser.parse_known_args()

    scenario = args.scenario

    if scenario == "no_ready":
        time.sleep(3600)
        return 0

    if scenario == "die_on_load":
        emit("ERR init")
        return 1

    if scenario == "noise":
        emit_noise()

    if args.stub_delay:
        time.sleep(args.stub_delay)

    emit(args.ready_token)

    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()

        if line == "QUIT":
            emit("OK bye")
            return 0

        if not line.startswith("INFER"):
            emit("ERR unknown_cmd")
            continue

        if scenario == "noise":
            emit_noise()

        if scenario == "die_on_infer":
            return 1

        if scenario == "hang_on_infer":
            time.sleep(3600)
            continue

        if scenario == "no_speech":
            emit("ERR no speech.")
            continue

        if scenario == "err_infer":
            emit("ERR infer")
            continue

        if args.ready_token == "READY tts":
            out_path = line.rsplit(" ", 1)[1]
            frames = write_wav(out_path, args.stub_rate, 0.2)
            emit(f"OK wav={out_path} frames={frames}")
            continue

        emit(f"OK language=english text={args.stub_text}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Then `chmod +x ambarella_asr_python/tests/stub_daemon.py`.

- [ ] **Step 2: Write the failing daemon test**

`ambarella_asr_python/tests/test_daemon.py`:

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import asyncio
import os
import sys
from unittest.mock import MagicMock

import pytest

from ambarella_asr_python.daemon import DaemonClient, DaemonError

STUB = os.path.join(os.path.dirname(__file__), "stub_daemon.py")


def make_client(scenario="ok", **kwargs):
    logger = MagicMock()
    return DaemonClient(
        bin_path=sys.executable,
        flags=[STUB, "--scenario", scenario],
        ready_token="READY asr",
        logger=logger,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_start_returns_the_ready_line():
    client = make_client()
    try:
        assert await client.start() == "READY asr"
        assert client.alive is True
        assert client.ready is True
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_request_returns_the_ok_line():
    client = make_client()
    try:
        await client.start()
        line = await client.request("INFER /tmp/x.wav", 10.0)
        assert line == "OK language=english text=hello world"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_err_is_returned_not_raised():
    client = make_client("no_speech")
    try:
        await client.start()
        assert await client.request("INFER /tmp/x.wav", 10.0) == (
            "ERR no speech."
        )
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_vendor_noise_is_skipped_not_parsed():
    client = make_client("noise")
    try:
        await client.start()
        line = await client.request("INFER /tmp/x.wav", 10.0)
        assert line.startswith("OK language=")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_noise_is_logged_under_the_vendor_category():
    client = make_client("noise")
    try:
        await client.start()
        assert client._log.log_debug.called
        _args, kwargs = client._log.log_debug.call_args
        assert kwargs["category"] == "vendor"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_missing_ready_times_out_with_a_daemon_error():
    client = make_client("no_ready", load_timeout_s=0.5)
    with pytest.raises(DaemonError, match="did not print"):
        await client.start()
    assert client.alive is False


@pytest.mark.asyncio
async def test_err_during_load_raises():
    client = make_client("die_on_load", load_timeout_s=5.0)
    with pytest.raises(DaemonError, match="ERR init"):
        await client.start()


@pytest.mark.asyncio
async def test_death_mid_request_raises():
    client = make_client("die_on_infer")
    try:
        await client.start()
        with pytest.raises(DaemonError, match="exited"):
            await client.request("INFER /tmp/x.wav", 10.0)
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_request_timeout_raises():
    client = make_client("hang_on_infer")
    try:
        await client.start()
        with pytest.raises(DaemonError, match="did not answer"):
            await client.request("INFER /tmp/x.wav", 0.5)
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_stop_sends_quit_and_reaps_the_process():
    client = make_client()
    await client.start()
    await client.stop()
    assert client.alive is False
    assert client.ready is False


@pytest.mark.asyncio
async def test_requests_are_serialised_under_the_lock():
    client = make_client()
    try:
        await client.start()
        lines = await asyncio.gather(
            client.request("INFER /tmp/a.wav", 10.0),
            client.request("INFER /tmp/b.wav", 10.0),
            client.request("INFER /tmp/c.wav", 10.0),
        )
        assert lines == ["OK language=english text=hello world"] * 3
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_wait_ready_reraises_a_background_start_failure():
    client = make_client("die_on_load", load_timeout_s=5.0)
    task = asyncio.create_task(client.start())
    await asyncio.sleep(0.5)
    with pytest.raises(DaemonError):
        await client.wait_ready(1.0)
    task.cancel()


@pytest.mark.asyncio
async def test_request_before_start_raises():
    client = make_client()
    with pytest.raises(DaemonError, match="not running"):
        await client.request("INFER /tmp/x.wav", 1.0)
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
docker exec ten_agent_dev bash -c "cd /app/agents/ten_packages/extension && python3 -m pytest ambarella_asr_python/tests/test_daemon.py -v"
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ambarella_asr_python.daemon'`.

- [ ] **Step 4: Write `daemon.py`**

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Client for the Ambarella resident speech daemons.

Both asr_d and tts_d load a model into Vector Processor memory at startup,
print a readiness token, then answer one command per line on stdin with one
terminal line on stdout. EazyAI logs to the same stdout, so any line that is
not a terminal line is vendor noise and is skipped.

Imports nothing from ten_ai_base, so these tests run outside the container.
"""

import asyncio
from typing import Any, Callable, List, Optional


class DaemonError(RuntimeError):
    """The daemon failed to become ready, died, or stopped answering.

    An `ERR ...` line in reply to a command is *not* this: it is a normal
    return value from `request()`, because `ERR no speech.` is how asr_d
    reports silence. Only load failures, death and timeouts raise.
    """


def _is_terminal(line: str) -> bool:
    return line.startswith("OK") or line.startswith("ERR")


class DaemonClient:
    """Owns one daemon child process and its request/response pipe."""

    def __init__(
        self,
        bin_path: str,
        flags: List[str],
        ready_token: str,
        logger: Any,
        load_timeout_s: float = 180.0,
        quit_timeout_s: float = 5.0,
        log_category: str = "vendor",
    ) -> None:
        self._bin_path = bin_path
        self._flags = list(flags)
        self._ready_token = ready_token
        self._log = logger
        self._load_timeout_s = load_timeout_s
        self._quit_timeout_s = quit_timeout_s
        self._log_category = log_category
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._start_error: Optional[DaemonError] = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self.alive

    async def start(self) -> str:
        """Spawn the daemon and wait for its readiness token.

        Callers run this as a background task: the model load takes tens of
        seconds and must not block session setup.
        """
        self._ready.clear()
        self._start_error = None
        self._proc = await asyncio.create_subprocess_exec(
            self._bin_path,
            *self._flags,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            line = await asyncio.wait_for(
                self._read_terminal(
                    lambda text: text.startswith(self._ready_token),
                    raise_on_err=True,
                ),
                self._load_timeout_s,
            )
        except asyncio.TimeoutError as err:
            self._start_error = DaemonError(
                f"{self._bin_path} did not print {self._ready_token!r} "
                f"within {self._load_timeout_s}s"
            )
            await self._kill()
            raise self._start_error from err
        except DaemonError as err:
            self._start_error = err
            await self._kill()
            raise
        self._ready.set()
        return line

    async def wait_ready(self, timeout: float) -> None:
        """Block until the background start() has finished, or re-raise it."""
        if self._start_error is not None:
            raise self._start_error
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError as err:
            if self._start_error is not None:
                raise self._start_error from err
            raise DaemonError(
                f"{self._bin_path} was not ready within {timeout}s"
            ) from err
        if self._start_error is not None:
            raise self._start_error

    async def request(self, command: str, timeout: float) -> str:
        """Send one command and return its terminal line."""
        async with self._lock:
            if not self.alive:
                raise DaemonError(f"{self._bin_path} is not running")
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write((command + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            try:
                return await asyncio.wait_for(
                    self._read_terminal(_is_terminal, raise_on_err=False),
                    timeout,
                )
            except asyncio.TimeoutError as err:
                verb = command.split(" ", 1)[0]
                raise DaemonError(
                    f"{self._bin_path} did not answer {verb} "
                    f"within {timeout}s"
                ) from err

    async def stop(self) -> None:
        """Send QUIT and reap the process, so the VP memory is released."""
        if not self.alive:
            self._proc = None
            self._ready.clear()
            return
        assert self._proc is not None
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.write(b"QUIT\n")
                await self._proc.stdin.drain()
            await asyncio.wait_for(self._proc.wait(), self._quit_timeout_s)
        except (
            asyncio.TimeoutError,
            BrokenPipeError,
            ConnectionResetError,
        ):
            await self._kill()
        finally:
            self._proc = None
            self._ready.clear()

    async def _read_line(self) -> str:
        assert self._proc is not None and self._proc.stdout is not None
        raw = await self._proc.stdout.readline()
        if not raw:
            raise DaemonError(
                f"{self._bin_path} exited "
                f"(returncode={self._proc.returncode})"
            )
        return raw.decode("utf-8", errors="replace").strip()

    async def _read_terminal(
        self, is_terminal: Callable[[str], bool], raise_on_err: bool
    ) -> str:
        while True:
            line = await self._read_line()
            if not line:
                continue
            if is_terminal(line):
                return line
            if raise_on_err and line.startswith("ERR"):
                raise DaemonError(line)
            self._log.log_debug(
                f"{self._ready_token}: {line}",
                category=self._log_category,
            )

    async def _kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            try:
                await asyncio.wait_for(self._proc.wait(), 5.0)
            except asyncio.TimeoutError:
                pass
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
docker exec ten_agent_dev bash -c "cd /app/agents/ten_packages/extension && python3 -m pytest ambarella_asr_python/tests/test_daemon.py -v"
```

Expected: PASS, 13 tests. If `test_noise_is_logged_under_the_vendor_category`
fails on `client._log`, the attribute name in `__init__` does not match — it
must be `self._log`.

- [ ] **Step 6: Format, lint and commit**

```bash
docker exec ten_agent_dev bash -c "cd /app && task format"
docker exec ten_agent_dev bash -c "cd /app && task lint-extension EXTENSION=ambarella_asr_python"
cd /root/ten-framework
git add ai_agents/agents/ten_packages/extension/ambarella_asr_python
git commit -m "feat: add the ambarella daemon line-protocol client"
```

---

### Task 3: The ASR extension

**Files:**
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/extension.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/addon.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/manifest.json`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/property.json`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/README.md`
- Test: `ai_agents/agents/ten_packages/extension/ambarella_asr_python/tests/test_extension.py`

**Interfaces:**
- Consumes: `AmbarellaASRConfig` (Task 1), `DaemonClient` / `DaemonError`
  (Task 2), all constants from `const.py`.
- Produces: `AmbarellaASRExtension(AsyncASRBaseExtension)` registered as
  `ambarella_asr_python`, with `vendor() -> "ambarella"`,
  `input_audio_sample_rate() -> 16000`,
  `buffer_strategy() -> ASRBufferConfigModeKeep(byte_limit=960_000)`, and the
  internal `async _infer_buffer() -> None` that tests drive directly.

- [ ] **Step 1: Write the failing extension test**

`ambarella_asr_python/tests/test_extension.py`:

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from ten_ai_base.asr import ASRBufferConfigModeKeep
from ten_ai_base.message import ModuleErrorCode

from ambarella_asr_python.const import BYTES_PER_SECOND, MAX_BUFFER_BYTES
from ambarella_asr_python.extension import AmbarellaASRExtension

STUB = os.path.join(os.path.dirname(__file__), "stub_daemon.py")


def make_env(scenario="ok", **overrides):
    config = {
        "bin_path": sys.executable,
        "model_dir": "/models/whisper",
        "params": {"scenario": scenario, "language": "english"},
    }
    config.update(overrides)
    env = AsyncMock()
    env.log_info = MagicMock()
    env.log_error = MagicMock()
    env.log_debug = MagicMock()
    env.log_warn = MagicMock()
    env.get_property_to_json = AsyncMock(
        return_value=(json.dumps(config), None)
    )
    return env


async def make_started(scenario="ok", **overrides):
    """Build an extension whose daemon is the stub, already ready."""
    extension = AmbarellaASRExtension("test_ambarella_asr")
    env = make_env(scenario, **overrides)
    extension.ten_env = env
    extension.send_asr_result = AsyncMock()
    extension.send_asr_error = AsyncMock()
    extension.send_asr_finalize_end = AsyncMock()
    await extension.on_init(env)
    # The stub is a script, so the interpreter is the binary and the script
    # path is the first flag.
    extension._stub_prefix = [STUB]
    await extension.start_connection()
    await extension.daemon.wait_ready(10.0)
    return extension


def speech(ms):
    return b"\x01\x00" * int(BYTES_PER_SECOND * ms / 1000 / 2)


def test_fixed_contract():
    extension = AmbarellaASRExtension("test")
    assert extension.vendor() == "ambarella"
    assert extension.input_audio_sample_rate() == 16000
    assert isinstance(extension.buffer_strategy(), ASRBufferConfigModeKeep)


@pytest.mark.asyncio
async def test_happy_path_emits_a_final_result():
    extension = await make_started()
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        assert extension.send_asr_result.await_count == 1
        result = extension.send_asr_result.await_args[0][0]
        assert result.text == "hello world"
        assert result.final is True
        assert result.language == "en-US"
        assert result.duration_ms == 1000
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_no_speech_is_not_an_error():
    extension = await make_started("no_speech")
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        assert extension.send_asr_result.await_count == 0
        assert extension.send_asr_error.await_count == 0
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_vendor_error_reports_and_still_finalizes():
    extension = await make_started("err_infer")
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        assert extension.send_asr_error.await_count == 1
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_timeout_reports_and_still_finalizes():
    extension = await make_started("hang_on_infer", infer_timeout_s=0.5)
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        assert extension.send_asr_error.await_count == 1
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_crash_reports_and_still_finalizes():
    extension = await make_started(
        "die_on_infer", restart_max_attempts=0
    )
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        assert extension.send_asr_error.await_count == 1
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_audio_below_the_floor_never_reaches_the_vp():
    extension = await make_started()
    try:
        extension._buffer.extend(speech(50))
        await extension.finalize(None)
        assert extension.send_asr_result.await_count == 0
        assert extension.send_asr_error.await_count == 0
        # The invariant holds even on the cheapest path.
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_empty_buffer_still_finalizes():
    extension = await make_started()
    try:
        await extension.finalize(None)
        assert extension.send_asr_finalize_end.await_count == 1
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_thirty_second_ceiling_infers_early_and_keeps_listening():
    extension = await make_started()
    try:
        frame = MagicMock()
        frame.lock_buf = MagicMock(return_value=bytearray(MAX_BUFFER_BYTES))
        frame.unlock_buf = MagicMock()
        assert await extension.send_audio(frame, None) is True
        assert extension.send_asr_result.await_count == 1
        # An early inference is not the end of a turn.
        assert extension.send_asr_finalize_end.await_count == 0
        assert len(extension._buffer) == 0
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_send_audio_accumulates_without_inferring():
    extension = await make_started()
    try:
        frame = MagicMock()
        frame.lock_buf = MagicMock(return_value=bytearray(speech(500)))
        frame.unlock_buf = MagicMock()
        await extension.send_audio(frame, None)
        assert len(extension._buffer) == len(speech(500))
        assert extension.send_asr_result.await_count == 0
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_start_times_are_monotonic_across_turns():
    extension = await make_started()
    try:
        extension._buffer.extend(speech(1000))
        await extension.finalize(None)
        extension._buffer.extend(speech(2000))
        await extension.finalize(None)
        first, second = [
            call[0][0] for call in extension.send_asr_result.await_args_list
        ]
        assert first.start_ms == 0
        assert second.start_ms == 1000
        assert second.duration_ms == 2000
    finally:
        await extension.stop_connection()


@pytest.mark.asyncio
async def test_load_failure_is_fatal():
    extension = AmbarellaASRExtension("test")
    env = make_env("die_on_load")
    extension.ten_env = env
    extension.send_asr_error = AsyncMock()
    await extension.on_init(env)
    extension._stub_prefix = [STUB]
    await extension.start_connection()
    await extension._start_task
    assert extension.send_asr_error.await_count == 1
    error = extension.send_asr_error.await_args[0][0]
    assert error.code == ModuleErrorCode.FATAL_ERROR.value
    await extension.stop_connection()


@pytest.mark.asyncio
async def test_missing_bin_path_is_fatal_at_init():
    extension = AmbarellaASRExtension("test")
    env = make_env(bin_path="")
    extension.ten_env = env
    extension.send_asr_error = AsyncMock()
    await extension.on_init(env)
    assert extension.send_asr_error.await_count == 1


@pytest.mark.asyncio
async def test_stop_removes_the_temp_wav():
    extension = await make_started()
    extension._buffer.extend(speech(1000))
    await extension.finalize(None)
    path = extension._wav_path
    assert os.path.exists(path)
    await extension.stop_connection()
    assert not os.path.exists(path)
```

Note the `_stub_prefix` hook: the stub is a Python script, so the process to
spawn is `sys.executable` with the script path prepended to the flags. Step 3
adds that seam to `start_connection()`.

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker exec ten_agent_dev bash -c "cd /app && task test-extension EXTENSION=agents/ten_packages/extension/ambarella_asr_python -- -k test_extension"
```

Expected: FAIL — `ModuleNotFoundError: No module named
'ambarella_asr_python.extension'`. This is the first command that needs
`ten_ai_base`, so it must run through `task test-extension` (which installs it
into `.ten/app`), not bare pytest.

- [ ] **Step 3: Write `extension.py`**

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import asyncio
import os
import uuid
import wave
from typing import List, Optional

from typing_extensions import override

from ten_ai_base.asr import (
    ASRBufferConfig,
    ASRBufferConfigModeKeep,
    ASRResult,
    AsyncASRBaseExtension,
)
from ten_ai_base.message import (
    ModuleError,
    ModuleErrorCode,
    ModuleErrorVendorInfo,
)
from ten_runtime import AsyncTenEnv, AudioFrame

from .config import AmbarellaASRConfig
from .const import (
    BYTES_PER_SECOND,
    LOG_CATEGORY_KEY_POINT,
    LOG_CATEGORY_VENDOR,
    MAX_BUFFER_BYTES,
    MODULE_NAME_ASR,
    NO_SPEECH_ERR,
    READY_TOKEN,
    SAMPLE_RATE,
)
from .daemon import DaemonClient, DaemonError

# asr_d treats these as "the user said nothing", which is not a failure.
_SILENCE_LINES = (NO_SPEECH_ERR, "ERR no_audio", "ERR audio_too_short")


class AmbarellaASRExtension(AsyncASRBaseExtension):
    """ASR over the on-board asr_d daemon.

    Unlike whisper_stt_python, inference happens only in finalize(): asr_d has
    no partial results, so transcribing a rolling window would burn VP time and
    chop sentences with nothing to stitch them back together.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: Optional[AmbarellaASRConfig] = None
        self.daemon: Optional[DaemonClient] = None
        self._buffer = bytearray()
        self._wav_path = ""
        self._start_task: Optional[asyncio.Task] = None
        self._restarts = 0
        self._elapsed_ms = 0
        # Test seam: the stub daemon is a script, so tests prepend its path.
        self._stub_prefix: List[str] = []

    @override
    def vendor(self) -> str:
        return "ambarella"

    @override
    def input_audio_sample_rate(self) -> int:
        # 16000 is compiled into asr_d and matches what RTC hands us after it
        # decodes G.722, so nothing is resampled on this path.
        return SAMPLE_RATE

    @override
    def buffer_strategy(self) -> ASRBufferConfig:
        # Agree with the daemon's own 30-second window, and catch the frames
        # that arrive while the model is still loading.
        return ASRBufferConfigModeKeep(byte_limit=MAX_BUFFER_BYTES)

    @override
    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)
        config_json, _ = await ten_env.get_property_to_json("")
        try:
            self.config = AmbarellaASRConfig.model_validate_json(config_json)
        except Exception as err:  # pylint: disable=broad-except
            ten_env.log_error(f"invalid ambarella_asr config: {err}")
            self.config = AmbarellaASRConfig()
            await self._fatal(str(err))
            return

        if not self.config.bin_path or not self.config.model_dir:
            await self._fatal("both bin_path and model_dir are required")
            return

        self._wav_path = os.path.join(
            self.config.tmp_dir,
            f"ambarella_asr_{uuid.uuid4().hex[:8]}.wav",
        )
        ten_env.log_info(
            f"ambarella_asr: bin={self.config.bin_path} "
            f"models={self.config.model_dir} "
            f"language={self.config.daemon_language}",
            category=LOG_CATEGORY_KEY_POINT,
        )

    @override
    async def start_connection(self) -> None:
        assert self.config is not None
        if self.daemon is not None:
            await self.stop_connection()
        self.daemon = DaemonClient(
            bin_path=self.config.bin_path,
            flags=self._stub_prefix + self.config.daemon_flags(),
            ready_token=READY_TOKEN,
            logger=self.ten_env,
            load_timeout_s=self.config.load_timeout_s,
            quit_timeout_s=self.config.quit_timeout_s,
            log_category=LOG_CATEGORY_VENDOR,
        )
        # The model load takes tens of seconds; letting it block here would
        # stall POST /start for the whole session.
        self._start_task = asyncio.create_task(self._start_daemon())

    async def _start_daemon(self) -> None:
        assert self.daemon is not None
        try:
            await self.daemon.start()
            self.ten_env.log_info(
                "asr_d is resident and ready",
                category=LOG_CATEGORY_KEY_POINT,
            )
        except DaemonError as err:
            # A load failure cannot be retried into success: the model path is
            # wrong, Cavalry is not loaded, or the VP is out of memory.
            await self._fatal(str(err))

    @override
    def is_connected(self) -> bool:
        return self.daemon is not None and self.daemon.alive

    @override
    async def stop_connection(self) -> None:
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
        self._start_task = None
        if self.daemon is not None:
            # QUIT is the only path that releases VP memory.
            await self.daemon.stop()
            self.daemon = None
        self._buffer.clear()
        if self._wav_path and os.path.exists(self._wav_path):
            os.unlink(self._wav_path)

    @override
    async def send_audio(
        self, frame: AudioFrame, _session_id: Optional[str]
    ) -> bool:
        if self.daemon is None:
            return False
        buf = frame.lock_buf()
        try:
            self._buffer.extend(bytes(buf))
        finally:
            frame.unlock_buf(buf)
        if len(self._buffer) >= MAX_BUFFER_BYTES:
            self.ten_env.log_info(
                "30 s window reached; inferring early and still listening"
            )
            # Not the end of a turn, so no finalize_end here.
            await self._infer_buffer()
        return True

    @override
    async def finalize(self, _session_id: Optional[str]) -> None:
        try:
            await self._infer_buffer()
        finally:
            # Every path must reach this, or main_control waits forever.
            await self.send_asr_finalize_end()

    async def _infer_buffer(self) -> None:
        assert self.config is not None
        audio = bytes(self._buffer)
        self._buffer.clear()

        duration_ms = len(audio) * 1000 // BYTES_PER_SECOND
        start_ms = self._elapsed_ms
        self._elapsed_ms += duration_ms

        floor_bytes = self.config.min_audio_ms * BYTES_PER_SECOND // 1000
        if len(audio) < floor_bytes:
            if audio:
                self.ten_env.log_debug(
                    f"{len(audio)} bytes is below min_audio_ms "
                    f"({self.config.min_audio_ms} ms); not sending to the VP"
                )
            return

        try:
            assert self.daemon is not None
            await self.daemon.wait_ready(self.config.load_timeout_s)
            self._write_wav(audio)
            line = await self.daemon.request(
                f"INFER {self._wav_path}", self.config.infer_timeout_s
            )
        except DaemonError as err:
            await self._on_daemon_error(err)
            return
        except (OSError, wave.Error) as err:
            await self._non_fatal(f"could not write {self._wav_path}: {err}")
            return

        if line in _SILENCE_LINES:
            self.ten_env.log_debug(f"no speech in {duration_ms} ms of audio")
            return

        if line.startswith("ERR"):
            await self._non_fatal(line)
            return

        if "text=" not in line:
            await self._non_fatal(f"unparseable asr_d reply: {line}")
            return

        text = line.split("text=", 1)[1].strip()
        if not text:
            return

        await self.send_asr_result(
            ASRResult(
                text=text,
                final=True,
                start_ms=start_ms,
                duration_ms=duration_ms,
                language=self.config.normalized_language,
                words=[],
            )
        )

    def _write_wav(self, audio: bytes) -> None:
        with wave.open(self._wav_path, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(audio)

    async def _fatal(self, message: str) -> None:
        await self.send_asr_error(
            ModuleError(
                module=MODULE_NAME_ASR,
                code=ModuleErrorCode.FATAL_ERROR.value,
                message=message,
            ),
            ModuleErrorVendorInfo(
                vendor=self.vendor(), code="load", message=message
            ),
        )

    async def _non_fatal(self, message: str) -> None:
        self.ten_env.log_error(
            f"vendor_error: {message}", category=LOG_CATEGORY_VENDOR
        )
        await self.send_asr_error(
            ModuleError(
                module=MODULE_NAME_ASR,
                code=ModuleErrorCode.NON_FATAL_ERROR.value,
                message=message,
            ),
            ModuleErrorVendorInfo(
                vendor=self.vendor(), code="infer", message=message
            ),
        )

    async def _on_daemon_error(self, err: DaemonError) -> None:
        assert self.config is not None
        await self._non_fatal(str(err))
        if self.daemon is not None and self.daemon.alive:
            return
        if self._restarts >= self.config.restart_max_attempts:
            self.ten_env.log_error(
                "asr_d is dead and the restart cap of "
                f"{self.config.restart_max_attempts} is spent"
            )
            return
        self._restarts += 1
        delay = float(2 ** (self._restarts - 1))
        self.ten_env.log_info(
            f"restarting asr_d, attempt {self._restarts}, after {delay}s"
        )
        await asyncio.sleep(delay)
        await self.start_connection()
```

- [ ] **Step 4: Write `addon.py`**

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
from ten_runtime import (
    Addon,
    register_addon_as_extension,
    TenEnv,
)

from .extension import AmbarellaASRExtension


@register_addon_as_extension("ambarella_asr_python")
class AmbarellaASRExtensionAddon(Addon):

    def on_create_instance(self, ten_env: TenEnv, name: str, context) -> None:
        ten_env.log_info("AmbarellaASRExtensionAddon on_create_instance")
        ten_env.on_create_instance_done(AmbarellaASRExtension(name), context)
```

- [ ] **Step 5: Write `manifest.json` and `property.json`**

`manifest.json` — the `name` must equal the addon decorator argument:

```json
{
  "type": "extension",
  "name": "ambarella_asr_python",
  "version": "0.1.0",
  "dependencies": [
    {
      "type": "system",
      "name": "ten_runtime_python",
      "version": "0.11"
    },
    {
      "type": "system",
      "name": "ten_ai_base",
      "version": "0.7"
    }
  ],
  "package": {
    "include": [
      "manifest.json",
      "property.json",
      "**.py",
      "README.md",
      "pyproject.toml",
      "requirements.txt",
      "tests/**"
    ]
  },
  "api": {
    "interface": [
      {
        "import_uri": "../../system/ten_ai_base/api/asr-interface.json"
      }
    ],
    "property": {
      "properties": {
        "bin_path": {
          "type": "string"
        },
        "model_dir": {
          "type": "string"
        },
        "model_type": {
          "type": "string"
        },
        "tmp_dir": {
          "type": "string"
        },
        "min_audio_ms": {
          "type": "int64"
        },
        "load_timeout_s": {
          "type": "float64"
        },
        "infer_timeout_s": {
          "type": "float64"
        },
        "quit_timeout_s": {
          "type": "float64"
        },
        "restart_max_attempts": {
          "type": "int64"
        },
        "params": {
          "type": "object",
          "properties": {}
        }
      }
    }
  }
}
```

`property.json`:

```json
{
    "bin_path": "/home/lychee/asr_tts_demo/app_demo/asr_d",
    "model_dir": "/home/lychee/asr_tts_demo/n1-655_whisper_tiny",
    "model_type": "tiny",
    "tmp_dir": "/tmp",
    "min_audio_ms": 200,
    "params": {
        "language": "chinese",
        "beam_size": 5,
        "no_speech_thres": 0.6,
        "log": 1
    }
}
```

- [ ] **Step 6: Write `README.md`**

```markdown
# ambarella_asr_python

ASR provider over `asr_d`, the resident Whisper-tiny daemon shipped in the
Ambarella AI Developer Kit. Runs natively on an N1-655 board.

## How it works

`asr_d` loads Whisper tiny into Vector Processor memory at startup, prints
`READY asr`, then answers `INFER <wav>` with `OK language=… text=…` in about
0.3 s. The extension spawns it as a child process, buffers incoming
`pcm_frame` audio, and issues one `INFER` per turn when the pipeline calls
`finalize()`.

Inference happens **only** on `finalize()`. `asr_d` emits no partial results,
so a rolling window would burn VP time and cut sentences with nothing to
reassemble them from. Put `ten_vad_python` in the graph so turns end.

## Prerequisites

On the board, before the agent starts:

```bash
cavalry_load -f /lib/firmware/cavalry.bin -r
chmod +x /home/lychee/asr_tts_demo/app_demo/asr_d
```

## Fixed properties of the daemon

| | Value | Configurable |
| --- | --- | --- |
| Input rate | 16000 Hz | No — compiled in |
| Format | PCM signed 16-bit, mono | No |
| Maximum audio per `INFER` | 30 s | No |
| Partial results | none | — |
| Language | fixed at startup | Per session only |

The 16 kHz input matches what RTC hands over after it decodes G.722, so
nothing is resampled on this path.

## Properties

| Property | Default | Notes |
| --- | --- | --- |
| `bin_path` | *required* | Absolute path to `asr_d` |
| `model_dir` | *required* | Becomes `--cavalry_dir` |
| `model_type` | `tiny` | Becomes `--type` |
| `tmp_dir` | `/tmp` | Holds the reused WAV |
| `min_audio_ms` | `200` | Shorter audio never reaches the VP |
| `load_timeout_s` | `180.0` | Model load allowance |
| `infer_timeout_s` | `30.0` | A breach means the daemon is wedged |
| `quit_timeout_s` | `5.0` | Then `kill()`, accepting a VP leak |
| `restart_max_attempts` | `3` | Each restart pays a full model load |
| `params` | see `property.json` | Expanded to `--key value` |

`params` is a pass-through onto `asr_d`'s startup flags: `language`,
`beam_size`, `no_speech_thres`, `log`. `cap_dev` is dropped deliberately —
it would enable `INFER_MIC`, which bypasses the TEN audio pipeline.

Language accepts TEN codes (`zh`, `zh-CN`, `en-US`) or the daemon's own words
(`chinese`, `english`, `auto`) and is reported back normalised.

## Tests

No hardware needed: `tests/stub_daemon.py` speaks the same line protocol, and
tests select its behaviour through `params.scenario`.

```bash
task test-extension EXTENSION=agents/ten_packages/extension/ambarella_asr_python
task test-extension-no-install EXTENSION=agents/ten_packages/extension/ambarella_asr_python -- -k test_daemon
```

## Design

`docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.md`
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
docker exec ten_agent_dev bash -c "cd /app && task test-extension EXTENSION=agents/ten_packages/extension/ambarella_asr_python"
```

Expected: PASS — 11 config, 13 daemon, 14 extension tests. Confirm all five
`finalize_end` paths are green: `test_happy_path_emits_a_final_result`,
`test_no_speech_is_not_an_error`, `test_vendor_error_reports_and_still_finalizes`,
`test_timeout_reports_and_still_finalizes`,
`test_crash_reports_and_still_finalizes`.

- [ ] **Step 8: Format, lint and commit**

```bash
docker exec ten_agent_dev bash -c "cd /app && task format"
docker exec ten_agent_dev bash -c "cd /app && task lint-extension EXTENSION=ambarella_asr_python"
cd /root/ten-framework
rm -rf ai_agents/agents/ten_packages/extension/ambarella_asr_python/.ten
git add ai_agents/agents/ten_packages/extension/ambarella_asr_python
git commit -m "feat: add the ambarella asr extension"
```

---

### Task 4: TTS package scaffolding, config and daemon client

**Files:**
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/__init__.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/const.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/config.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/daemon.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/pyproject.toml`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/requirements.txt`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/tests/__init__.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/tests/bin/start`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/tests/stub_daemon.py`
- Test: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/tests/test_config.py`

**Interfaces:**
- Consumes: nothing from the ASR package — `daemon.py` and `stub_daemon.py` are
  copied verbatim, which is the deliberate duplication of spec §3.
- Produces: `AmbarellaTTSConfig(AsyncTTS2HttpConfig)` with `bin_path`,
  `model_dir`, `tmp_dir`, `max_chars`, `output_sample_rate`, timeouts,
  `restart_max_attempts`, `dump`, `dump_path`, `params`, plus
  `daemon_flags() -> list[str]`, `update_params() -> None`,
  `to_str(sensitive_handling: bool = True) -> str`, `validate() -> None`.
  Constants `READY_TOKEN = "READY tts"`, `NATIVE_SAMPLE_RATE = 22050`,
  `OUTPUT_SAMPLE_RATE = 16000`, `MODULE_NAME_TTS = "tts"`.

- [ ] **Step 1: Create the skeleton and copy the daemon client**

```bash
cd /root/ten-framework/ai_agents/agents/ten_packages/extension
mkdir -p ambarella_tts_python/tests/bin
cp ambarella_asr_python/daemon.py ambarella_tts_python/daemon.py
cp ambarella_asr_python/tests/stub_daemon.py ambarella_tts_python/tests/stub_daemon.py
cp ambarella_asr_python/tests/bin/start ambarella_tts_python/tests/bin/start
cp ambarella_asr_python/tests/__init__.py ambarella_tts_python/tests/__init__.py
chmod +x ambarella_tts_python/tests/bin/start ambarella_tts_python/tests/stub_daemon.py
```

`daemon.py` needs no edits — the readiness token is a constructor argument.

`ambarella_tts_python/__init__.py`:

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
from . import addon
```

`ambarella_tts_python/pyproject.toml`:

```toml
[project]
name = "ambarella-tts-python"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "numpy>=1.24.0",
    "scipy",
]
```

`ambarella_tts_python/requirements.txt`:

```
numpy>=1.24.0
scipy
```

- [ ] **Step 2: Write `const.py`**

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

MODULE_NAME_TTS = "tts"

LOG_CATEGORY_VENDOR = "vendor"
LOG_CATEGORY_KEY_POINT = "key_point"

# tts_d prints this once OpenVoice is resident in VP memory.
READY_TOKEN = "READY tts"

# 22050 is compiled into tts_d's SF_INFO (movz x6, #22050) alongside
# channels=1 and SF_FORMAT_WAV|SF_FORMAT_PCM_16. None of it is configurable.
NATIVE_SAMPLE_RATE = 22050

# The transport is 16 kHz G.722 both ways, and the RTSA SDK is told its PCM
# rate once at init rather than per frame -- so the conversion happens here.
OUTPUT_SAMPLE_RATE = 16000

# 20 ms of 16 kHz PCM16 mono.
CHUNK_BYTES = OUTPUT_SAMPLE_RATE * 2 * 20 // 1000

# Sentence-ending punctuation used to split one over-long sentence.
SENTENCE_MARKS = "。！？；.!?;,，"
```

- [ ] **Step 3: Write the failing config test**

`ambarella_tts_python/tests/test_config.py`:

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import pytest

from ambarella_tts_python.config import AmbarellaTTSConfig
from ambarella_tts_python.const import (
    CHUNK_BYTES,
    NATIVE_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
)


def test_fixed_rates():
    assert NATIVE_SAMPLE_RATE == 22050
    assert OUTPUT_SAMPLE_RATE == 16000
    assert CHUNK_BYTES == 640


def test_defaults():
    config = AmbarellaTTSConfig(
        bin_path="/opt/tts_d", model_dir="/models/openvoice"
    )
    assert config.tmp_dir == "/tmp"
    assert config.max_chars == 200
    assert config.output_sample_rate == 16000
    assert config.load_timeout_s == 180.0
    assert config.infer_timeout_s == 30.0
    assert config.quit_timeout_s == 5.0
    assert config.restart_max_attempts == 3


def test_flags_carry_model_dir():
    config = AmbarellaTTSConfig(
        bin_path="/opt/tts_d", model_dir="/models/openvoice"
    )
    flags = config.daemon_flags()
    assert flags[:2] == ["--model_dir", "/models/openvoice"]


def test_flags_expand_params_deterministically():
    config = AmbarellaTTSConfig(
        bin_path="/b",
        model_dir="/m",
        params={"speaker_id": 3, "rand_seed": 42},
    )
    assert config.daemon_flags() == [
        "--model_dir",
        "/m",
        "--log",
        "1",
        "--rand_seed",
        "42",
        "--speaker_id",
        "3",
    ]


def test_log_defaults_to_error_level():
    config = AmbarellaTTSConfig(bin_path="/b", model_dir="/m")
    flags = config.daemon_flags()
    assert flags[flags.index("--log") + 1] == "1"


def test_validate_requires_bin_path_and_model_dir():
    with pytest.raises(ValueError, match="bin_path"):
        AmbarellaTTSConfig(model_dir="/m").validate()
    with pytest.raises(ValueError, match="model_dir"):
        AmbarellaTTSConfig(bin_path="/b").validate()


def test_validate_accepts_a_complete_config():
    AmbarellaTTSConfig(bin_path="/b", model_dir="/m").validate()
```

- [ ] **Step 4: Run the test to verify it fails**

```bash
docker exec ten_agent_dev bash -c "cd /app && task test-extension EXTENSION=agents/ten_packages/extension/ambarella_tts_python -- -k test_config"
```

Expected: FAIL — `ModuleNotFoundError: No module named
'ambarella_tts_python.config'`. This config subclasses `AsyncTTS2HttpConfig`,
so it needs `ten_ai_base` and must run through `task test-extension`.

- [ ] **Step 5: Write `config.py`**

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import copy
from typing import Any, Dict, List

from pydantic import Field
from ten_ai_base.tts2_http import AsyncTTS2HttpConfig

from .const import OUTPUT_SAMPLE_RATE


class AmbarellaTTSConfig(AsyncTTS2HttpConfig):
    """Configuration for the on-board tts_d daemon."""

    bin_path: str = ""
    model_dir: str = ""
    tmp_dir: str = "/tmp"

    # One over-long sentence is split on punctuation so the first audio does
    # not wait for the whole synthesis.
    max_chars: int = 200

    # Declared to the pipeline. tts_d always emits 22050; the resampling ratio
    # is derived from the WAV header on every response.
    output_sample_rate: int = OUTPUT_SAMPLE_RATE

    load_timeout_s: float = 180.0
    infer_timeout_s: float = 30.0
    quit_timeout_s: float = 5.0
    restart_max_attempts: int = 3

    dump: bool = Field(default=False)
    dump_path: str = Field(default="/tmp/ambarella_tts_out.pcm")
    params: Dict[str, Any] = Field(default_factory=dict)

    def daemon_flags(self) -> List[str]:
        """Build tts_d's argv from model_dir plus the params pass-through."""
        flags = ["--model_dir", self.model_dir]
        params = dict(self.params)
        params.setdefault("log", 1)
        for key in sorted(params):
            flags.extend([f"--{key}", str(params[key])])
        return flags

    def update_params(self) -> None:
        """Coerce the numeric pass-through params tts_d validates."""
        for key in ("speaker_id", "rand_seed", "log"):
            if key in self.params:
                self.params[key] = int(self.params[key])

    def to_str(self, sensitive_handling: bool = True) -> str:
        """Render the config. The board interface has no credentials."""
        if not sensitive_handling:
            return f"{self}"
        return f"{copy.deepcopy(self)}"

    def validate(self) -> None:
        if not self.bin_path:
            raise ValueError("bin_path is required for Ambarella TTS")
        if not self.model_dir:
            raise ValueError("model_dir is required for Ambarella TTS")
```

- [ ] **Step 6: Copy the daemon test and run both**

```bash
cd /root/ten-framework/ai_agents/agents/ten_packages/extension
sed -e 's/ambarella_asr_python/ambarella_tts_python/g' \
    -e 's/READY asr/READY tts/g' \
    ambarella_asr_python/tests/test_daemon.py \
    > ambarella_tts_python/tests/test_daemon.py
```

Then open `ambarella_tts_python/tests/test_daemon.py` and change the two
assertions that name ASR replies so they match the TTS stub, which writes a WAV
and answers `OK wav=… frames=…`:

```python
@pytest.mark.asyncio
async def test_request_returns_the_ok_line():
    client = make_client()
    try:
        await client.start()
        line = await client.request("INFER hello /tmp/stub_out.wav", 10.0)
        assert line.startswith("OK wav=/tmp/stub_out.wav frames=")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_requests_are_serialised_under_the_lock():
    client = make_client()
    try:
        await client.start()
        lines = await asyncio.gather(
            client.request("INFER a /tmp/stub_a.wav", 10.0),
            client.request("INFER b /tmp/stub_b.wav", 10.0),
            client.request("INFER c /tmp/stub_c.wav", 10.0),
        )
        assert all(line.startswith("OK wav=") for line in lines)
    finally:
        await client.stop()
```

Also change `test_vendor_noise_is_skipped_not_parsed` to
`assert line.startswith("OK wav=")` and pass an output path in its `INFER`.

```bash
docker exec ten_agent_dev bash -c "cd /app && task test-extension EXTENSION=agents/ten_packages/extension/ambarella_tts_python"
```

Expected: PASS — 7 config, 13 daemon tests.

- [ ] **Step 7: Format, lint and commit**

```bash
docker exec ten_agent_dev bash -c "cd /app && task format"
docker exec ten_agent_dev bash -c "cd /app && task lint-extension EXTENSION=ambarella_tts_python"
cd /root/ten-framework
git add ai_agents/agents/ten_packages/extension/ambarella_tts_python
git commit -m "feat: add ambarella tts package scaffolding and config"
```

---

### Task 5: The TTS client — synthesis, WAV readback and resampling

**Files:**
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/ambarella_tts.py`
- Test: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/tests/test_client.py`

**Interfaces:**
- Consumes: `AmbarellaTTSConfig` (Task 4), `DaemonClient` / `DaemonError`
  (Task 4), constants from `const.py`.
- Produces: `AmbarellaTTSClient(AsyncTTS2HttpClient)` with
  `__init__(config: AmbarellaTTSConfig, ten_env: AsyncTenEnv)`,
  `async get(text: str, request_id: str) -> AsyncIterator[tuple[bytes | None,
  TTS2HttpResponseEventType]]`, `async cancel() -> None`,
  `async clean() -> None`, `get_extra_metadata() -> dict[str, Any]`,
  `async start() -> None`, and the module functions
  `sanitise(text: str) -> str` and `split_text(text: str, max_chars: int) ->
  list[str]`.

- [ ] **Step 1: Write the failing client test**

`ambarella_tts_python/tests/test_client.py`:

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import os
import struct
import sys
import wave
from unittest.mock import AsyncMock, MagicMock

import pytest
from ten_ai_base.tts2_http import TTS2HttpResponseEventType

from ambarella_tts_python.ambarella_tts import (
    AmbarellaTTSClient,
    sanitise,
    split_text,
)
from ambarella_tts_python.config import AmbarellaTTSConfig
from ambarella_tts_python.const import OUTPUT_SAMPLE_RATE
from ambarella_tts_python.daemon import DaemonError

STUB = os.path.join(os.path.dirname(__file__), "stub_daemon.py")


def make_client(scenario="ok", rate=22050, **overrides):
    fields = {
        "bin_path": sys.executable,
        "model_dir": "/models/openvoice",
        "params": {
            "scenario": scenario,
            "stub-rate": rate,
            "speaker_id": 0,
        },
    }
    fields.update(overrides)
    config = AmbarellaTTSConfig(**fields)
    env = AsyncMock()
    env.log_info = MagicMock()
    env.log_error = MagicMock()
    env.log_debug = MagicMock()
    env.log_warn = MagicMock()
    client = AmbarellaTTSClient(config=config, ten_env=env)
    client._stub_prefix = [STUB]
    return client


async def drain(client, text="hello there", request_id="r1"):
    chunks = []
    events = []
    async for payload, event in client.get(text, request_id):
        chunks.append(payload)
        events.append(event)
    return chunks, events


def test_sanitise_folds_newlines_that_would_break_the_protocol():
    assert sanitise("one\ntwo") == "one two"
    assert sanitise("one\r\ntwo\tthree") == "one two three"
    assert sanitise("  spaced   out  ") == "spaced out"
    assert sanitise("\n\n\t ") == ""


def test_sanitise_keeps_inner_spaces():
    assert sanitise("hello world") == "hello world"


def test_split_text_returns_one_piece_when_short():
    assert split_text("hello world", 200) == ["hello world"]


def test_split_text_splits_a_long_sentence_on_punctuation():
    text = "a" * 120 + ", " + "b" * 120 + ". " + "c" * 20
    pieces = split_text(text, 200)
    assert len(pieces) >= 2
    assert "".join(p.replace(" ", "") for p in pieces) == text.replace(" ", "")


def test_split_text_hard_splits_when_there_is_no_punctuation():
    text = "x" * 500
    pieces = split_text(text, 200)
    assert all(len(p) <= 200 for p in pieces)
    assert "".join(pieces) == text


@pytest.mark.asyncio
async def test_empty_text_ends_without_touching_the_daemon():
    client = make_client()
    try:
        chunks, events = await drain(client, "   \n  ")
        assert events == [TTS2HttpResponseEventType.END]
        assert chunks == [None]
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_audio_is_resampled_to_sixteen_kilohertz():
    client = make_client(rate=22050)
    try:
        chunks, events = await drain(client)
        assert events[-1] == TTS2HttpResponseEventType.END
        audio = b"".join(c for c in chunks if c)
        # The stub synthesises 0.2 s; at 16 kHz PCM16 that is 6400 bytes.
        # Allow a few samples of filter delay either way.
        assert abs(len(audio) - 6400) < 400
        assert len(audio) % 2 == 0
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_native_rate_is_passed_through_when_it_already_matches():
    client = make_client(rate=16000)
    try:
        chunks, _events = await drain(client)
        audio = b"".join(c for c in chunks if c)
        assert len(audio) == 6400
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_a_header_rate_other_than_native_is_logged_loudly():
    client = make_client(rate=24000)
    try:
        await drain(client)
        messages = [
            call[0][0] for call in client.ten_env.log_error.call_args_list
        ]
        assert any("24000" in m for m in messages)
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_chunks_are_twenty_milliseconds():
    client = make_client()
    try:
        chunks, _events = await drain(client)
        audio_chunks = [c for c in chunks if c]
        assert all(len(c) <= 640 for c in audio_chunks)
        assert len(audio_chunks[0]) == 640
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_cancel_flushes_without_killing_the_daemon():
    client = make_client()
    try:
        await client.start()
        await client.cancel()
        chunks, events = await drain(client)
        assert events[-1] == TTS2HttpResponseEventType.FLUSH
        assert chunks[-1] is None
        assert client._daemon.alive is True
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_vendor_error_yields_an_error_event():
    client = make_client("err_infer")
    try:
        _chunks, events = await drain(client)
        assert TTS2HttpResponseEventType.ERROR in events
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_synthesis_output_rate_is_declared_as_sixteen_k():
    client = make_client()
    assert client.config.output_sample_rate == OUTPUT_SAMPLE_RATE
    await client.clean()


@pytest.mark.asyncio
async def test_metadata_reports_the_voice():
    client = make_client()
    client.config.params["speaker_id"] = 3
    assert client.get_extra_metadata()["speaker_id"] == 3
    await client.clean()


@pytest.mark.asyncio
async def test_respawns_are_capped():
    client = make_client(restart_max_attempts=1)
    try:
        # Two spawns are allowed, the third is refused rather than paying a
        # third model load.
        await client.start()
        await client._daemon.stop()
        await client.start()
        await client._daemon.stop()
        with pytest.raises(DaemonError, match="restart cap"):
            await client.start()
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_warm_up_swallows_a_load_failure():
    client = make_client("die_on_load")
    try:
        await client.warm_up()
        messages = [
            call[0][0] for call in client.ten_env.log_error.call_args_list
        ]
        assert any("warm up" in m for m in messages)
    finally:
        await client.clean()


@pytest.mark.asyncio
async def test_clean_removes_the_temp_wav():
    client = make_client()
    await drain(client)
    path = client._wav_path
    assert os.path.exists(path)
    await client.clean()
    assert not os.path.exists(path)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker exec ten_agent_dev bash -c "cd /app && task test-extension-no-install EXTENSION=agents/ten_packages/extension/ambarella_tts_python -- -k test_client"
```

Expected: FAIL — `ModuleNotFoundError: No module named
'ambarella_tts_python.ambarella_tts'`.

- [ ] **Step 3: Write `ambarella_tts.py`**

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""TTS over the on-board tts_d daemon.

tts_d synthesises a whole WAV per INFER at a hardcoded 22050 Hz. The transport
is 16 kHz G.722 and the RTSA SDK is told its PCM rate once at initialisation
rather than per frame, so this client converts to 16000 before yielding.
"""

import math
import os
import re
import uuid
import wave
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import numpy as np
from scipy.signal import resample_poly
from ten_ai_base.tts2_http import (
    AsyncTTS2HttpClient,
    TTS2HttpResponseEventType,
)
from ten_runtime import AsyncTenEnv

from .config import AmbarellaTTSConfig
from .const import (
    CHUNK_BYTES,
    LOG_CATEGORY_KEY_POINT,
    LOG_CATEGORY_VENDOR,
    NATIVE_SAMPLE_RATE,
    READY_TOKEN,
    SENTENCE_MARKS,
)
from .daemon import DaemonClient, DaemonError

_WHITESPACE = re.compile(r"\s+")


def sanitise(text: str) -> str:
    """Fold every whitespace run into a single space.

    Mandatory, not defensive: the protocol is one command per line with the
    last whitespace token as the output path, so a newline in LLM output would
    end the command early and leave the remainder to be read as a second,
    unparseable command -- desynchronising the pipe for good.
    """
    return _WHITESPACE.sub(" ", text).strip()


def split_text(text: str, max_chars: int) -> List[str]:
    """Split one over-long sentence on punctuation, then hard-split."""
    if len(text) <= max_chars:
        return [text]

    pieces: List[str] = []
    current = ""
    for char in text:
        current += char
        if char in SENTENCE_MARKS and len(current) >= max_chars // 2:
            pieces.append(current.strip())
            current = ""
    if current.strip():
        pieces.append(current.strip())

    hard: List[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            hard.append(piece)
            continue
        for start in range(0, len(piece), max_chars):
            hard.append(piece[start : start + max_chars])
    return [piece for piece in hard if piece]


class AmbarellaTTSClient(AsyncTTS2HttpClient):
    """Drives one resident tts_d process."""

    def __init__(
        self, config: AmbarellaTTSConfig, ten_env: AsyncTenEnv
    ) -> None:
        super().__init__()
        self.config = config
        self.ten_env = ten_env
        self._is_cancelled = False
        self._daemon: Optional[DaemonClient] = None
        self._wav_path = os.path.join(
            config.tmp_dir, f"ambarella_tts_{uuid.uuid4().hex[:8]}.wav"
        )
        self._warned_rate = False
        self._starts = 0
        # Test seam: the stub daemon is a script, so tests prepend its path.
        self._stub_prefix: List[str] = []

    async def warm_up(self) -> None:
        """Load the model in the background, so the first turn does not.

        Never raises: it runs as a detached task, and get() reports the same
        failure through the ERROR event if the load really is broken.
        """
        try:
            await self.start()
        except DaemonError as err:
            self.ten_env.log_error(
                f"tts_d failed to warm up: {err}",
                category=LOG_CATEGORY_VENDOR,
            )

    async def start(self) -> None:
        """Spawn tts_d if it is not already resident.

        Every spawn pays a full model load, so respawns are capped the same
        way the ASR side caps them.
        """
        if self._daemon is not None and self._daemon.alive:
            return
        if self._starts > self.config.restart_max_attempts:
            raise DaemonError(
                "tts_d is dead and the restart cap of "
                f"{self.config.restart_max_attempts} is spent"
            )
        self._starts += 1
        self._daemon = DaemonClient(
            bin_path=self.config.bin_path,
            flags=self._stub_prefix + self.config.daemon_flags(),
            ready_token=READY_TOKEN,
            logger=self.ten_env,
            load_timeout_s=self.config.load_timeout_s,
            quit_timeout_s=self.config.quit_timeout_s,
            log_category=LOG_CATEGORY_VENDOR,
        )
        await self._daemon.start()
        self.ten_env.log_info(
            "tts_d is resident and ready", category=LOG_CATEGORY_KEY_POINT
        )

    async def cancel(self) -> None:
        """Stop yielding audio. Never kill the daemon.

        tts_d's INFER cannot be interrupted, but it only runs ~0.3 s. Killing
        the process on barge-in would cost a full model reload, and barge-in is
        routine in a voice pipeline.
        """
        self.ten_env.log_debug("AmbarellaTTS: cancel() called")
        self._is_cancelled = True

    async def clean(self) -> None:
        if self._daemon is not None:
            await self._daemon.stop()
            self._daemon = None
        if os.path.exists(self._wav_path):
            os.unlink(self._wav_path)

    def get_extra_metadata(self) -> Dict[str, Any]:
        return {
            "speaker_id": self.config.params.get("speaker_id", 0),
            "rand_seed": self.config.params.get("rand_seed", -1),
        }

    async def get(
        self, text: str, request_id: str
    ) -> AsyncIterator[Tuple[Optional[bytes], TTS2HttpResponseEventType]]:
        self._is_cancelled = False

        clean_text = sanitise(text)
        if not clean_text:
            self.ten_env.log_warn(
                f"AmbarellaTTS: empty text for request_id {request_id}"
            )
            yield None, TTS2HttpResponseEventType.END
            return

        try:
            await self.start()
        except DaemonError as err:
            yield str(err).encode("utf-8"), TTS2HttpResponseEventType.ERROR
            yield None, TTS2HttpResponseEventType.END
            return

        for piece in split_text(clean_text, self.config.max_chars):
            if self._is_cancelled:
                yield None, TTS2HttpResponseEventType.FLUSH
                return

            try:
                assert self._daemon is not None
                line = await self._daemon.request(
                    f"INFER {piece} {self._wav_path}",
                    self.config.infer_timeout_s,
                )
            except DaemonError as err:
                self.ten_env.log_error(
                    f"vendor_error: {err}", category=LOG_CATEGORY_VENDOR
                )
                yield str(err).encode(
                    "utf-8"
                ), TTS2HttpResponseEventType.ERROR
                yield None, TTS2HttpResponseEventType.END
                return

            if line.startswith("ERR"):
                self.ten_env.log_error(
                    f"vendor_error: {line}", category=LOG_CATEGORY_VENDOR
                )
                yield line.encode("utf-8"), TTS2HttpResponseEventType.ERROR
                yield None, TTS2HttpResponseEventType.END
                return

            try:
                pcm = self._read_and_resample()
            except (OSError, wave.Error) as err:
                yield str(err).encode(
                    "utf-8"
                ), TTS2HttpResponseEventType.ERROR
                yield None, TTS2HttpResponseEventType.END
                return

            for start in range(0, len(pcm), CHUNK_BYTES):
                if self._is_cancelled:
                    yield None, TTS2HttpResponseEventType.FLUSH
                    return
                yield pcm[
                    start : start + CHUNK_BYTES
                ], TTS2HttpResponseEventType.RESPONSE

        yield None, TTS2HttpResponseEventType.END

    def _read_and_resample(self) -> bytes:
        """Read tts_d's WAV back and convert it to the declared rate."""
        with wave.open(self._wav_path, "rb") as source:
            header_rate = source.getframerate()
            frames = source.readframes(source.getnframes())

        if header_rate != NATIVE_SAMPLE_RATE and not self._warned_rate:
            self._warned_rate = True
            # A different rate means the binary or the model was swapped, and
            # a fixed 320/441 ratio would then be wrong.
            self.ten_env.log_error(
                f"tts_d wrote {header_rate} Hz, not the expected "
                f"{NATIVE_SAMPLE_RATE} Hz; the resampling ratio is being "
                "derived from the header instead",
                category=LOG_CATEGORY_VENDOR,
            )

        target = self.config.output_sample_rate
        if header_rate == target:
            return frames

        samples = np.frombuffer(frames, dtype=np.int16)
        if samples.size == 0:
            return b""

        divisor = math.gcd(target, header_rate)
        up = target // divisor
        down = header_rate // divisor
        # resample_poly applies an FIR anti-aliasing filter. np.interp, used
        # elsewhere in this repo, does not -- and this is a downsample, so
        # without the filter the 8-11 kHz content folds back audibly.
        resampled = resample_poly(samples.astype(np.float32), up, down)
        clipped = np.clip(resampled, -32768.0, 32767.0)
        return clipped.astype(np.int16).tobytes()
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker exec ten_agent_dev bash -c "cd /app && task test-extension EXTENSION=agents/ten_packages/extension/ambarella_tts_python -- -k test_client"
```

Expected: PASS, 17 tests.

- [ ] **Step 5: Format, lint and commit**

```bash
docker exec ten_agent_dev bash -c "cd /app && task format"
docker exec ten_agent_dev bash -c "cd /app && task lint-extension EXTENSION=ambarella_tts_python"
cd /root/ten-framework
git add ai_agents/agents/ten_packages/extension/ambarella_tts_python
git commit -m "feat: add the ambarella tts synthesis client"
```

---

### Task 6: The TTS extension

**Files:**
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/extension.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/addon.py`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/manifest.json`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/property.json`
- Create: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/README.md`
- Test: `ai_agents/agents/ten_packages/extension/ambarella_tts_python/tests/test_extension.py`

**Interfaces:**
- Consumes: `AmbarellaTTSConfig` (Task 4), `AmbarellaTTSClient` (Task 5),
  `OUTPUT_SAMPLE_RATE` (Task 4).
- Produces: `AmbarellaTTSExtension(AsyncTTS2HttpExtension)` registered as
  `ambarella_tts_python`, with `async create_config(config_json_str: str) ->
  AsyncTTS2HttpConfig`, `async create_client(config, ten_env) ->
  AsyncTTS2HttpClient`, `vendor() -> "ambarella"`, and
  `synthesize_audio_sample_rate() -> int`.

- [ ] **Step 1: Write the failing extension test**

`ambarella_tts_python/tests/test_extension.py`:

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ambarella_tts_python.ambarella_tts import AmbarellaTTSClient
from ambarella_tts_python.config import AmbarellaTTSConfig
from ambarella_tts_python.extension import AmbarellaTTSExtension

CONFIG = json.dumps(
    {
        "bin_path": "/opt/tts_d",
        "model_dir": "/models/openvoice",
        "params": {"speaker_id": 0},
    }
)


def test_vendor_name():
    assert AmbarellaTTSExtension("test").vendor() == "ambarella"


@pytest.mark.asyncio
async def test_create_config_parses_the_property_json():
    extension = AmbarellaTTSExtension("test")
    config = await extension.create_config(CONFIG)
    assert isinstance(config, AmbarellaTTSConfig)
    assert config.bin_path == "/opt/tts_d"
    assert config.model_dir == "/models/openvoice"


@pytest.mark.asyncio
async def test_create_client_returns_the_ambarella_client():
    extension = AmbarellaTTSExtension("test")
    config = await extension.create_config(CONFIG)
    env = AsyncMock()
    env.log_info = MagicMock()
    env.log_debug = MagicMock()
    client = await extension.create_client(config, env)
    assert isinstance(client, AmbarellaTTSClient)


@pytest.mark.asyncio
async def test_declared_rate_is_sixteen_k():
    extension = AmbarellaTTSExtension("test")
    extension.config = await extension.create_config(CONFIG)
    assert extension.synthesize_audio_sample_rate() == 16000


@pytest.mark.asyncio
async def test_declared_rate_follows_the_property():
    extension = AmbarellaTTSExtension("test")
    extension.config = await extension.create_config(
        json.dumps(
            {
                "bin_path": "/b",
                "model_dir": "/m",
                "output_sample_rate": 22050,
            }
        )
    )
    assert extension.synthesize_audio_sample_rate() == 22050
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker exec ten_agent_dev bash -c "cd /app && task test-extension-no-install EXTENSION=agents/ten_packages/extension/ambarella_tts_python -- -k test_extension"
```

Expected: FAIL — `ModuleNotFoundError: No module named
'ambarella_tts_python.extension'`.

- [ ] **Step 3: Write `extension.py`**

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
"""Ambarella TTS extension.

The board's tts_d is a request/response provider, so this rides
AsyncTTS2HttpExtension -- the same base polly_tts uses. Its contract is
transport-agnostic despite the name.
"""

import asyncio

from ten_ai_base.tts2_http import (
    AsyncTTS2HttpClient,
    AsyncTTS2HttpConfig,
    AsyncTTS2HttpExtension,
)
from ten_runtime import AsyncTenEnv

from .ambarella_tts import AmbarellaTTSClient
from .config import AmbarellaTTSConfig
from .const import OUTPUT_SAMPLE_RATE


class AmbarellaTTSExtension(AsyncTTS2HttpExtension):
    """TTS over the on-board tts_d daemon."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: AmbarellaTTSConfig = None
        self.client: AmbarellaTTSClient = None
        self._warm_up_task = None

    async def create_config(
        self, config_json_str: str
    ) -> AsyncTTS2HttpConfig:
        return AmbarellaTTSConfig.model_validate_json(config_json_str)

    async def create_client(
        self, config: AsyncTTS2HttpConfig, ten_env: AsyncTenEnv
    ) -> AsyncTTS2HttpClient:
        client = AmbarellaTTSClient(config=config, ten_env=ten_env)
        # OpenVoice takes tens of seconds to reach the VP. Warm it in the
        # background so the first spoken sentence does not pay for it, and
        # keep a reference so the task is not garbage-collected mid-load.
        self._warm_up_task = asyncio.create_task(client.warm_up())
        return client

    def vendor(self) -> str:
        return "ambarella"

    def synthesize_audio_sample_rate(self) -> int:
        # tts_d always writes 22050; the client resamples to this before
        # yielding, because the RTSA SDK is told its PCM rate once at init.
        if self.config is None:
            return OUTPUT_SAMPLE_RATE
        return int(self.config.output_sample_rate)
```

- [ ] **Step 4: Write `addon.py`**

```python
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
from ten_runtime import (
    Addon,
    register_addon_as_extension,
    TenEnv,
)

from .extension import AmbarellaTTSExtension


@register_addon_as_extension("ambarella_tts_python")
class AmbarellaTTSExtensionAddon(Addon):

    def on_create_instance(self, ten_env: TenEnv, name: str, context) -> None:
        ten_env.log_info("AmbarellaTTSExtensionAddon on_create_instance")
        ten_env.on_create_instance_done(AmbarellaTTSExtension(name), context)
```

- [ ] **Step 5: Write `manifest.json` and `property.json`**

`manifest.json`:

```json
{
  "type": "extension",
  "name": "ambarella_tts_python",
  "version": "0.1.0",
  "dependencies": [
    {
      "type": "system",
      "name": "ten_runtime_python",
      "version": "0.11"
    },
    {
      "type": "system",
      "name": "ten_ai_base",
      "version": "0.7"
    }
  ],
  "package": {
    "include": [
      "manifest.json",
      "property.json",
      "**.py",
      "README.md",
      "pyproject.toml",
      "requirements.txt",
      "tests/**"
    ]
  },
  "api": {
    "interface": [
      {
        "import_uri": "../../system/ten_ai_base/api/tts-interface.json"
      }
    ],
    "property": {
      "properties": {
        "bin_path": {
          "type": "string"
        },
        "model_dir": {
          "type": "string"
        },
        "tmp_dir": {
          "type": "string"
        },
        "max_chars": {
          "type": "int64"
        },
        "output_sample_rate": {
          "type": "int64"
        },
        "load_timeout_s": {
          "type": "float64"
        },
        "infer_timeout_s": {
          "type": "float64"
        },
        "quit_timeout_s": {
          "type": "float64"
        },
        "restart_max_attempts": {
          "type": "int64"
        },
        "dump": {
          "type": "bool"
        },
        "dump_path": {
          "type": "string"
        },
        "params": {
          "type": "object",
          "properties": {}
        }
      }
    }
  }
}
```

If `task install` rejects the `import_uri`, list the installed interface files
and use the exact filename:

```bash
docker exec ten_agent_dev bash -c "ls /app/agents/examples/voice-assistant/tenapp/ten_packages/system/ten_ai_base/api/"
```

`property.json`:

```json
{
    "bin_path": "/home/lychee/asr_tts_demo/app_demo/tts_d",
    "model_dir": "/home/lychee/asr_tts_demo/n1-655_openvoice",
    "tmp_dir": "/tmp",
    "max_chars": 200,
    "output_sample_rate": 16000,
    "params": {
        "speaker_id": 0,
        "rand_seed": -1,
        "log": 1
    }
}
```

- [ ] **Step 6: Write `README.md`**

```markdown
# ambarella_tts_python

TTS provider over `tts_d`, the resident OpenVoice daemon shipped in the
Ambarella AI Developer Kit. Runs natively on an N1-655 board.

## How it works

`tts_d` loads OpenVoice into Vector Processor memory at startup, prints
`READY tts`, then answers `INFER <text> <out.wav>` with
`OK wav=… frames=…` in about 0.3 s. The extension spawns it as a child
process, reads the WAV back, resamples it and yields PCM in 20 ms chunks.

## Prerequisites

On the board, before the agent starts:

```bash
cavalry_load -f /lib/firmware/cavalry.bin -r
chmod +x /home/lychee/asr_tts_demo/app_demo/tts_d
```

`tts_d` also needs `libsndfile.so.1`, which `asr_d` does not — the vendor
README lists it as a shared prerequisite, which is misleading.

## Sample rate

`tts_d` writes a hardcoded 22050 Hz PCM16 mono WAV: the constant and the
`SF_FORMAT_WAV|SF_FORMAT_PCM_16` format flag each appear once in its `.text`
with no flag reaching them.

The transport is 16 kHz G.722, and the RTSA SDK is told its PCM input rate
**once** at service initialisation rather than per frame. A session declared at
16000 would read 22050-sampled data as 16000 and play it slow and low-pitched
with no error raised, so this extension converts to 16000 itself using
`scipy.signal.resample_poly(x, 320, 441)` — an exact ratio. G.722's passband
tops out near 7 kHz, so the downsample discards nothing the codec would have
kept.

The WAV header is checked on every response; a rate other than 22050 is logged
loudly and the ratio is recomputed from the header.

## Properties

| Property | Default | Notes |
| --- | --- | --- |
| `bin_path` | *required* | Absolute path to `tts_d` |
| `model_dir` | *required* | Becomes `--model_dir` |
| `tmp_dir` | `/tmp` | Holds the reused WAV |
| `max_chars` | `200` | Punctuation split for one long sentence |
| `output_sample_rate` | `16000` | Declared to the pipeline |
| `load_timeout_s` | `180.0` | Model load allowance |
| `infer_timeout_s` | `30.0` | A breach means the daemon is wedged |
| `quit_timeout_s` | `5.0` | Then `kill()`, accepting a VP leak |
| `restart_max_attempts` | `3` | Each restart pays a full model load |
| `params` | see `property.json` | Expanded to `--key value` |

`params` carries `speaker_id` (0-9, the voice), `rand_seed` and `log`. All
three are startup flags, so **the voice is fixed for a session** and cannot be
changed per request.

## Barge-in

`cancel()` sets a flag and stops yielding; it never kills the daemon. `tts_d`'s
`INFER` is not interruptible, but it only runs ~0.3 s — whereas killing the
process would cost a full model reload on every barge-in.

## Tests

```bash
task test-extension EXTENSION=agents/ten_packages/extension/ambarella_tts_python
```

## Design

`docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.md`
```

- [ ] **Step 7: Run the whole package suite**

```bash
docker exec ten_agent_dev bash -c "cd /app && task test-extension EXTENSION=agents/ten_packages/extension/ambarella_tts_python"
```

Expected: PASS — 7 config, 13 daemon, 17 client, 5 extension tests.

- [ ] **Step 8: Format, lint and commit**

```bash
docker exec ten_agent_dev bash -c "cd /app && task format"
docker exec ten_agent_dev bash -c "cd /app && task lint-extension EXTENSION=ambarella_tts_python"
cd /root/ten-framework
rm -rf ai_agents/agents/ten_packages/extension/ambarella_tts_python/.ten
git add ai_agents/agents/ten_packages/extension/ambarella_tts_python
git commit -m "feat: add the ambarella tts extension"
```

---

### Task 7: The example graph

**Files:**
- Create: `ai_agents/agents/examples/voice-assistant-ambarella/tenapp/manifest.json`
- Create: `ai_agents/agents/examples/voice-assistant-ambarella/tenapp/property.json`
- Create: `ai_agents/agents/examples/voice-assistant-ambarella/README.md`
- Copy: `Taskfile.yml`, `Taskfile.docker.yml`, `Dockerfile`, `tenapp/main.go`,
  `tenapp/go.mod`, `tenapp/go.sum`, `tenapp/.tenignore` from
  `voice-assistant-with-ten-vad`

**Interfaces:**
- Consumes: the addon names `ambarella_asr_python` (Task 3),
  `ambarella_tts_python` (Task 6), and the existing `ambarella_llm2_python`.
- Produces: a graph named `voice_assistant` whose nodes are `agora_rtc`,
  `streamid_adapter`, `vad`, `stt`, `llm`, `tts`, `main_control`,
  `message_collector`.

- [ ] **Step 1: Copy the scaffolding from the ten-vad example**

```bash
cd /root/ten-framework/ai_agents/agents/examples
mkdir -p voice-assistant-ambarella/tenapp
cp voice-assistant-with-ten-vad/Taskfile.yml voice-assistant-ambarella/
cp voice-assistant-with-ten-vad/Taskfile.docker.yml voice-assistant-ambarella/
cp voice-assistant-with-ten-vad/Dockerfile voice-assistant-ambarella/
cp voice-assistant-with-ten-vad/tenapp/main.go voice-assistant-ambarella/tenapp/
cp voice-assistant-with-ten-vad/tenapp/go.mod voice-assistant-ambarella/tenapp/
cp voice-assistant-with-ten-vad/tenapp/go.sum voice-assistant-ambarella/tenapp/
cp voice-assistant-with-ten-vad/tenapp/.tenignore voice-assistant-ambarella/tenapp/
```

Do **not** copy `manifest-lock.json` — it is a generated artifact.

- [ ] **Step 2: Write `tenapp/manifest.json`**

Only the extensions this graph actually instantiates, so `task install` stays
fast on the board:

```json
{
  "type": "app",
  "name": "voice_assistant_ambarella",
  "version": "0.1.0",
  "dependencies": [
    {
      "type": "system",
      "name": "ten_runtime_go",
      "version": "0.11"
    },
    {
      "type": "extension",
      "name": "agora_rtc",
      "version": "=0.23.9-t1"
    },
    {
      "type": "system",
      "name": "ten_ai_base",
      "version": "0.7"
    },
    {
      "path": "../../../ten_packages/extension/streamid_adapter"
    },
    {
      "path": "../../../ten_packages/extension/ten_vad_python"
    },
    {
      "path": "../../../ten_packages/extension/ambarella_asr_python"
    },
    {
      "path": "../../../ten_packages/extension/ambarella_llm2_python"
    },
    {
      "path": "../../../ten_packages/extension/ambarella_tts_python"
    },
    {
      "path": "../../../ten_packages/extension/message_collector2"
    }
  ],
  "scripts": {
    "start": "bash tools/run.sh"
  }
}
```

If `voice-assistant-with-ten-vad/tenapp/manifest.json` carries a different
`scripts` block or app `name` convention, match it rather than this sample:

```bash
docker exec ten_agent_dev bash -c "cd /app && python3 -c \"import json;d=json.load(open('agents/examples/voice-assistant-with-ten-vad/tenapp/manifest.json'));print(json.dumps({k:v for k,v in d.items() if k!='dependencies'},indent=2))\""
```

- [ ] **Step 3: Write `tenapp/property.json`**

```json
{
    "ten": {
        "predefined_graphs": [
            {
                "name": "voice_assistant",
                "auto_start": false,
                "graph": {
                    "nodes": [
                        {
                            "type": "extension",
                            "name": "agora_rtc",
                            "addon": "agora_rtc",
                            "extension_group": "default",
                            "property": {
                                "app_id": "${env:AGORA_APP_ID}",
                                "app_certificate": "${env:AGORA_APP_CERTIFICATE|}",
                                "channel": "ten_agent_test",
                                "stream_id": 1234,
                                "remote_stream_id": 123,
                                "subscribe_audio": true,
                                "publish_audio": true,
                                "publish_data": true,
                                "enable_agora_asr": false
                            }
                        },
                        {
                            "type": "extension",
                            "name": "streamid_adapter",
                            "addon": "streamid_adapter",
                            "extension_group": "default"
                        },
                        {
                            "type": "extension",
                            "name": "vad",
                            "addon": "ten_vad_python",
                            "extension_group": "default",
                            "property": {}
                        },
                        {
                            "type": "extension",
                            "name": "stt",
                            "addon": "ambarella_asr_python",
                            "extension_group": "default",
                            "property": {
                                "bin_path": "/home/lychee/asr_tts_demo/app_demo/asr_d",
                                "model_dir": "/home/lychee/asr_tts_demo/n1-655_whisper_tiny",
                                "params": {
                                    "language": "chinese",
                                    "beam_size": 5,
                                    "no_speech_thres": 0.6,
                                    "log": 1
                                }
                            }
                        },
                        {
                            "type": "extension",
                            "name": "llm",
                            "addon": "ambarella_llm2_python",
                            "extension_group": "default",
                            "property": {
                                "base_url": "http://127.0.0.1:8080",
                                "model_type": 9,
                                "prompt": "You are a helpful assistant running on an Ambarella N1-655 board. Keep answers short and spoken.",
                                "response_format": "auto"
                            }
                        },
                        {
                            "type": "extension",
                            "name": "tts",
                            "addon": "ambarella_tts_python",
                            "extension_group": "default",
                            "property": {
                                "bin_path": "/home/lychee/asr_tts_demo/app_demo/tts_d",
                                "model_dir": "/home/lychee/asr_tts_demo/n1-655_openvoice",
                                "output_sample_rate": 16000,
                                "params": {
                                    "speaker_id": 0,
                                    "rand_seed": -1,
                                    "log": 1
                                }
                            }
                        },
                        {
                            "type": "extension",
                            "name": "main_control",
                            "addon": "main_python",
                            "extension_group": "default",
                            "property": {
                                "greeting": "TEN Agent connected. How can I help you today?"
                            }
                        },
                        {
                            "type": "extension",
                            "name": "message_collector",
                            "addon": "message_collector2",
                            "extension_group": "default"
                        }
                    ],
                    "connections": [
                        {
                            "extension": "main_control",
                            "cmd": [
                                {
                                    "names": [
                                        "on_user_joined",
                                        "on_user_left"
                                    ],
                                    "source": [
                                        {
                                            "extension": "agora_rtc"
                                        }
                                    ]
                                },
                                {
                                    "names": [
                                        "start_of_sentence",
                                        "end_of_sentence"
                                    ],
                                    "source": [
                                        {
                                            "extension": "vad"
                                        }
                                    ]
                                }
                            ],
                            "data": [
                                {
                                    "name": "asr_result",
                                    "source": [
                                        {
                                            "extension": "stt"
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            "extension": "agora_rtc",
                            "audio_frame": [
                                {
                                    "name": "pcm_frame",
                                    "dest": [
                                        {
                                            "extension": "streamid_adapter"
                                        }
                                    ]
                                },
                                {
                                    "name": "pcm_frame",
                                    "source": [
                                        {
                                            "extension": "tts"
                                        }
                                    ]
                                }
                            ],
                            "data": [
                                {
                                    "name": "data",
                                    "source": [
                                        {
                                            "extension": "message_collector"
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            "extension": "streamid_adapter",
                            "audio_frame": [
                                {
                                    "name": "pcm_frame",
                                    "dest": [
                                        {
                                            "extension": "stt"
                                        },
                                        {
                                            "extension": "vad"
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            }
        ]
    }
}
```

Three things this graph gets right on purpose:

- The LLM node is named `llm`. `main_control` addresses `chat_completion` and
  `abort` to that name; renaming it breaks the turn loop silently.
- The audio split to `stt` and `vad` happens at `streamid_adapter`, the source
  — splitting further downstream drops frames (`docs/ai/L1/07_gotchas.md`,
  "Audio Routing: Split at Source Only").
- `vad` sends `start_of_sentence` / `end_of_sentence` to `main_control`, which
  is what eventually drives the ASR extension's `finalize()`. Without the VAD
  node no turn ever ends and `asr_d` is never asked to infer.

- [ ] **Step 4: Verify the graph parses and the three names line up**

```bash
cd /root/ten-framework
python3 - <<'CHECK'
import json
from pathlib import Path

prop = json.load(
    open(
        "ai_agents/agents/examples/voice-assistant-ambarella/tenapp/property.json"
    )
)
manifest = json.load(
    open(
        "ai_agents/agents/examples/voice-assistant-ambarella/tenapp/manifest.json"
    )
)
graph = prop["ten"]["predefined_graphs"][0]["graph"]
nodes = {n["name"]: n["addon"] for n in graph["nodes"]}
print("nodes:", nodes)

assert nodes["llm"] == "ambarella_llm2_python", "main_control needs a node named llm"

ext_root = Path("ai_agents/agents/ten_packages/extension")
declared = {
    Path(d["path"]).name for d in manifest["dependencies"] if "path" in d
}
for name, addon in nodes.items():
    if addon in ("agora_rtc", "main_python"):
        continue
    assert addon in declared, f"{addon} is not a path dependency"
    src = ext_root / addon
    assert (src / "manifest.json").exists(), f"{addon} has no manifest"
    assert json.load(open(src / "manifest.json"))["name"] == addon, (
        f"{addon}: manifest name does not match the graph addon field"
    )
    decorator = f'@register_addon_as_extension("{addon}")'
    assert decorator in (src / "addon.py").read_text(), (
        f"{addon}: addon.py decorator does not match"
    )
print("all three names match for every local extension")
CHECK
```

Expected: prints the node map and `all three names match for every local
extension`.

- [ ] **Step 5: Write `README.md`**

```markdown
# voice-assistant-ambarella

A voice agent that runs entirely on an Ambarella N1-655 board: ASR, LLM and TTS
are all on-board daemons, with Agora RTC as the transport.

```
G.722 16k ──► agora_rtc ──► streamid_adapter ──┬──► stt   (asr_d, 16k in)
                                               └──► vad   (turn boundaries)
                                                      │
                                     main_control ◄───┘
                                          │
                                          ▼
                    llm (board LLM demo server) ──► tts (tts_d) ──► agora_rtc
```

## Before starting

1. Load the Cavalry firmware and make the daemons executable:

   ```bash
   cavalry_load -f /lib/firmware/cavalry.bin -r
   chmod +x /home/lychee/asr_tts_demo/app_demo/{asr_d,tts_d}
   ```

2. Start the board's LLM demo server:

   ```bash
   cd /usr/share/ambarella/llm_demo/
   ./run_llm_demo.sh --run_mode start --model_type 9 \
       --model_path ~/demo_resources/llm_demo --ip 127.0.0.1 --max_user 1
   ```

   The first load after boot takes up to 80 s; wait for `Device ENABLE` in
   `/tmp/log.txt`.

3. **Move TEN's API server off port 8080.** The LLM demo binds 8080 and
   `run_llm_demo.sh` has no port option, so TEN must yield:

   ```bash
   export SERVER_PORT=8090
   ```

4. Do not run the LLM demo and the LLaVA demo together — they share a library
   that does not support it.

## Running

```bash
task install
task run
```

## Sample rates

16 kHz throughout, in both directions. RTC decodes G.722 to 16 kHz PCM, which
is exactly what `asr_d` wants. On the way back, `tts_d` emits 22050 Hz and
`ambarella_tts_python` resamples to 16000 before handing frames to RTC.

## Known constraints

- **Language and voice are fixed per session.** `asr_d`'s `--language` and
  `tts_d`'s `--speaker_id` are startup flags.
- **No partial ASR results.** `asr_d` is batch-only, so text appears once per
  turn, after the VAD closes it.
- **`--max_user 1`** on the LLM demo means one turn at a time.
- **Adding or removing a graph is not hot-reloadable.** Restart the server and
  the playground; the frontend caches `/graphs`.

## Design

`docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.md`
```

- [ ] **Step 6: Commit**

```bash
cd /root/ten-framework
git add ai_agents/agents/examples/voice-assistant-ambarella
git commit -m "feat: add the on-board ambarella voice assistant example"
```

---

### Task 8: The VP concurrency measurement

The spec (§9) makes this a required deliverable, not an optional check: nothing
in the vendor material shows the two daemons inferring **simultaneously**, which
is exactly what a barge-in causes. This task answers the question with a number
and records it.

**Files:**
- Create: `tools/ambarella/vp_concurrency_probe.py`
- Modify: `docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.md`
  (append the measured result to §9)

**Interfaces:**
- Consumes: the real `asr_d` and `tts_d` binaries on a board. Nothing from the
  extensions — the probe is deliberately standalone so a failure implicates the
  hardware, not the Python.
- Produces: a printed table of sequential versus concurrent latency, and a
  recommendation on whether a cross-extension lock is needed.

- [ ] **Step 1: Write the probe**

```python
#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Measure whether asr_d and tts_d can infer at the same time.

test_alternate.py in the vendor demo proves the two daemons can be resident
together and infer alternately. It never overlaps them. A voice pipeline does:
a user barging in while TTS is synthesising puts an ASR INFER on top of a TTS
INFER. Run this on the board before trusting that path.

Usage:
  python3 vp_concurrency_probe.py \
      --asr-bin  /home/lychee/asr_tts_demo/app_demo/asr_d \
      --tts-bin  /home/lychee/asr_tts_demo/app_demo/tts_d \
      --whisper  /home/lychee/asr_tts_demo/n1-655_whisper_tiny \
      --openvoice /home/lychee/asr_tts_demo/n1-655_openvoice \
      --wav      /home/lychee/asr_tts_demo/app_demo/sample_16k_mono.wav \
      --rounds   10
"""

import argparse
import asyncio
import statistics
import time
from typing import List, Optional, Tuple


async def spawn(binary: str, flags: List[str], token: str):
    proc = await asyncio.create_subprocess_exec(
        binary,
        *flags,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            raise RuntimeError(f"{binary} exited before printing {token}")
        line = raw.decode("utf-8", errors="replace").strip()
        if line.startswith(token):
            return proc
        if line.startswith("ERR"):
            raise RuntimeError(f"{binary}: {line}")
        print(f"  [{token}] {line}")


async def infer(proc, command: str) -> Tuple[float, str]:
    started = time.monotonic()
    proc.stdin.write((command + "\n").encode("utf-8"))
    await proc.stdin.drain()
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            raise RuntimeError("daemon exited mid-inference")
        line = raw.decode("utf-8", errors="replace").strip()
        if line.startswith("OK") or line.startswith("ERR"):
            return time.monotonic() - started, line


async def quit_daemon(proc) -> None:
    proc.stdin.write(b"QUIT\n")
    await proc.stdin.drain()
    try:
        await asyncio.wait_for(proc.wait(), 10.0)
    except asyncio.TimeoutError:
        proc.kill()


def report(label: str, samples: List[float]) -> None:
    print(
        f"  {label:26} n={len(samples):<3} "
        f"mean={statistics.mean(samples) * 1000:7.1f} ms  "
        f"max={max(samples) * 1000:7.1f} ms"
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr-bin", required=True)
    parser.add_argument("--tts-bin", required=True)
    parser.add_argument("--whisper", required=True)
    parser.add_argument("--openvoice", required=True)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args()

    print("Loading both daemons into VP memory...")
    asr = await spawn(
        args.asr_bin,
        [
            "--cavalry_dir",
            args.whisper,
            "--type",
            "tiny",
            "--language",
            "english",
            "--log",
            "1",
        ],
        "READY asr",
    )
    tts = await spawn(
        args.tts_bin,
        ["--model_dir", args.openvoice, "--speaker_id", "0", "--log", "1"],
        "READY tts",
    )
    print("Both resident.\n")

    seq_asr: List[float] = []
    seq_tts: List[float] = []
    con_asr: List[float] = []
    con_tts: List[float] = []
    failures: List[str] = []

    print(f"Baseline: {args.rounds} alternating rounds")
    for index in range(args.rounds):
        elapsed, line = await infer(asr, f"INFER {args.wav}")
        seq_asr.append(elapsed)
        if line.startswith("ERR") and line != "ERR no speech.":
            failures.append(f"sequential asr round {index}: {line}")
        elapsed, line = await infer(
            tts, f"INFER baseline round {index} /tmp/probe_seq_{index}.wav"
        )
        seq_tts.append(elapsed)
        if line.startswith("ERR"):
            failures.append(f"sequential tts round {index}: {line}")

    print(f"\nOverlapped: {args.rounds} simultaneous rounds")
    for index in range(args.rounds):
        asr_task = asyncio.create_task(infer(asr, f"INFER {args.wav}"))
        tts_task = asyncio.create_task(
            infer(
                tts,
                f"INFER overlapped round {index} /tmp/probe_con_{index}.wav",
            )
        )
        (asr_ms, asr_line), (tts_ms, tts_line) = await asyncio.gather(
            asr_task, tts_task
        )
        con_asr.append(asr_ms)
        con_tts.append(tts_ms)
        if asr_line.startswith("ERR") and asr_line != "ERR no speech.":
            failures.append(f"concurrent asr round {index}: {asr_line}")
        if tts_line.startswith("ERR"):
            failures.append(f"concurrent tts round {index}: {tts_line}")

    await quit_daemon(asr)
    await quit_daemon(tts)

    print("\nResults")
    report("asr, alternating", seq_asr)
    report("asr, overlapped", con_asr)
    report("tts, alternating", seq_tts)
    report("tts, overlapped", con_tts)

    asr_ratio = statistics.mean(con_asr) / statistics.mean(seq_asr)
    tts_ratio = statistics.mean(con_tts) / statistics.mean(seq_tts)
    print(f"\n  asr slowdown when overlapped: {asr_ratio:.2f}x")
    print(f"  tts slowdown when overlapped: {tts_ratio:.2f}x")

    print("\nVerdict")
    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        print(
            "  Concurrent inference FAILS on this board. Serialise VP access "
            "with a module-level asyncio.Lock shared by both extensions, and "
            "record the added barge-in latency."
        )
        return 1
    if max(asr_ratio, tts_ratio) > 2.0:
        print(
            "  Concurrent inference works but costs more than 2x. Consider "
            "serialising, and measure the barge-in latency either way."
        )
        return 0
    print(
        "  Concurrent inference is safe and cheap on this board. No "
        "cross-extension lock is needed; spec section 9 is resolved."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run it on the board**

```bash
python3 tools/ambarella/vp_concurrency_probe.py \
    --asr-bin /home/lychee/asr_tts_demo/app_demo/asr_d \
    --tts-bin /home/lychee/asr_tts_demo/app_demo/tts_d \
    --whisper /home/lychee/asr_tts_demo/n1-655_whisper_tiny \
    --openvoice /home/lychee/asr_tts_demo/n1-655_openvoice \
    --wav /home/lychee/asr_tts_demo/app_demo/sample_16k_mono.wav \
    --rounds 10
```

Expected: a results table and one of the three verdicts. Exit code 1 means
concurrent inference fails and the design needs the lock discussed in spec §9.

- [ ] **Step 3: Record the answer in the spec**

Replace the "VP concurrency is unverified" paragraph in §9 of
`docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.md` with
the measured result: the four latency figures, the two slowdown ratios, the
verdict, and the date and board the numbers came from. Do the same in the
matching `<div class="bad">` block of the `.html` edition, and move the row
`| Simultaneous inference | **Not verified** — see §9 |` in §12 to cite the
probe instead.

If the verdict was FAIL, stop and re-open the design: a module-level
`asyncio.Lock` shared by both extensions is the fix, and it changes the
barge-in latency budget enough to need its own review.

- [ ] **Step 4: Commit**

```bash
cd /root/ten-framework
git add tools/ambarella/vp_concurrency_probe.py \
        docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.md \
        docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.html
git commit -F - <<'MSG'
test: measure ambarella vp concurrency on the board

test_alternate.py in the vendor demo only ever alternates the two daemons. A
barge-in overlaps them, so this probe measures the overlapped case directly
and the spec now records the number instead of the open question.
MSG
```

---

## Full-suite gate

Run before opening a pull request. Each command maps to a blocking CI job.

- [ ] `docker exec ten_agent_dev bash -c "cd /app && task check"` — black, 80 columns
- [ ] `docker exec ten_agent_dev bash -c "cd /app && task lint"` — pylint over every extension, any warning is fatal
- [ ] `docker exec ten_agent_dev bash -c "cd /app && task test -- -s -v"` — the command CI runs
- [ ] `git log --format=%B origin/main..HEAD | awk 'length > 100 {print; exit 1}'` — no body line over 100 characters
- [ ] `git log --format=%B origin/main..HEAD | grep -iE "co-authored|claude|anthropic|generated with"` — must find nothing

If `task check` reports reformatting for files you did not touch, a stale
`.ten/` is shadowing the source: `rm -rf ai_agents/agents/ten_packages/extension/ambarella_*/.ten`.
