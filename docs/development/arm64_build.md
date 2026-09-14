# TEN Framework on arm64

A working manual for running and building TEN Framework on aarch64 Linux:
what the framework is made of, which parts are architecture-dependent, how to
build each of them, and how to find out what went wrong.

Written against `feat/arm64-native-build`. Every claim here was checked against
the source or run on a machine; where something could not be verified it says
so rather than guessing.

An HTML edition of this manual with eleven diagrams is alongside it at
[`arm64_build.html`](arm64_build.html) — the same text, plus figures for the
mechanisms that are hard to hold in prose (addon loading, the Python ABI
mismatch, the RTC two-package split, `${env:}` resolution, log routing).
A Traditional Chinese edition of the same manual is at
[`arm64_build.zh-TW.html`](arm64_build.zh-TW.html).

**Contents**

| Part | Covers |
| ---- | ------ |
| I — Architecture | The layer stack, how extensions talk to each other, how the runtime loads them |
| II — Platform | The ABI floor, which shared object does what, the build system and its flags |
| III — Building | The framework core, the agents on three distribution families, and `agora_rtc` |
| IV — Development | Writing and maintaining an extension, version pinning, swapping vendors, testing |
| V — Operations | The scripts, where the logs are, and diagnosing by symptom |
| VI — Status | What is left for upstream, and what this manual does not cover |

**Where to start**

- Running the agents on an arm64 box → Part III, then Part V
- RTC does not work → Part III, "Building agora_rtc for aarch64"
- Something fails and the UI says nothing → Part V, "Diagnosing by symptom"
- Writing an extension → Part IV
- Deciding whether a change is arm64-specific → Part II

---

# Part I — Architecture

## 1. The layer stack

A vendor extension is Python-only and sits at the top of four layers:

```
extension  (Python)          deepgram_asr_python, elevenlabs_tts2_python, …
    ↑
ten_ai_base                  AsyncASRBaseExtension, AsyncTTS2BaseExtension,
                             AsyncLLM2BaseExtension, AsyncLLMToolBaseExtension
    ↑
language binding             core/src/ten_runtime/binding/{python,go,nodejs}
    ↑
C runtime                    core/src/ten_runtime
```

Adding a vendor touches only the top layer. Changing a message type, a schema or
a lifecycle contract reaches into `core/` — a different build, a different CI
job, and a different half of the repository.

`ten_ai_base` is **not source in this repo**. It is a versioned registry
dependency materialised by `tman install`, so the base-class paths quoted
throughout `docs/ai/` only exist inside an installed tenapp. Searching a fresh
checkout for them comes up empty.

## 2. The two halves of the repository

| | Framework core | AI agents |
| --- | --- | --- |
| Paths | `core/`, `packages/`, `tests/`, `build/`, `third_party/`, `tools/` | `ai_agents/` |
| Languages | C/C++ runtime, Rust (`ten_rust`, `ten_manager`), Go/Python/Node bindings | Python extensions, Go API server, Next.js playground |
| Build | GN + Ninja through the `tgn` wrapper | `task` targets |
| CI | `linux_ubuntu2204.yml`, `mac_*`, `win.yml`, `tman_full_*` | `ai_agents.yaml` |

The two CI sets use mutually exclusive `paths-ignore` filters: a change under
`ai_agents/` never triggers the core build, and a change under `core/` never
triggers the agents job.

Most of this manual concerns the agents half. Part III's first chapter is the
only one about building the core.

## 3. Messages and routing

Extensions never call each other. Every interaction is a message, and the
graph's `connections` block decides who receives it. That is why swapping a
vendor is a JSON edit, and why a mistyped name fails silently.

### 3.1 Five message types

Source: `core/include/ten_runtime/msg/msg.h`

| Type | Semantics | Typical use |
| ---- | --------- | ----------- |
| `cmd` | **Has a reply** — the sender receives a `CmdResult` | `tool_register`, `on_user_joined` |
| `cmd_result` | The reply, paired by the runtime | never routed by hand |
| `data` | One-way, structured | `asr_result`, `metrics`, `tts_flush_end` |
| `audio_frame` | One-way, zero-copy audio buffer | `pcm_frame` |
| `video_frame` | One-way, video buffer | vision extensions |

The runtime also defines internal commands — `START_GRAPH`, `STOP_GRAPH`,
`CLOSE_APP`, `TIMER`, `TIMEOUT`, `TRIGGER_LIFE_CYCLE` — which the framework uses
itself.

### 3.2 The connections block

Routing lives in `tenapp/property.json` under
`predefined_graphs[].graph.connections`. Each block hangs off one node, keyed by
the node's **`name`**, not its addon:

```json
{
  "extension": "main_control",
  "cmd":  [ { "names": ["tool_register"], "source": [{"extension": "weatherapi_tool_python"}] } ],
  "data": [ { "name": "asr_result",       "source": [{"extension": "stt"}] } ]
}
```

Direction comes from `source` or `dest`, and both are usable:

- `source` — these nodes send to *me* (subscribe)
- `dest` — *I* send to these nodes (publish)

The same link may be written from either end, so reading a graph means checking
both. `cmd` takes `names` (an array); `data` and `audio_frame` take `name`.

### 3.3 Three names must match

An extension is instantiated only if all three agree. They do not, it fails
**silently at graph load** — no error, the extension simply is not there:

1. the `@register_addon_as_extension("x")` argument in `addon.py`
2. the `name` field in that extension's `manifest.json`
3. the `addon` field of the graph node

A node's `name` is a separate thing: the instance's label within the graph, and
what `connections` routes by. When swapping a vendor, change `addon` and **leave
`name` alone** — renaming it detaches the node from the pipeline without
complaint.

### 3.4 Unrouted messages are dropped

Sending a message the graph has no connection for produces a warning and
nothing else:

```
W ten:runtime ten_env_send_msg_internal@send.c:163
  Failed to send message: Failed to find destination of a 'data' message 'error' from graph.
```

**That warning is throttled, and the throttle changes how you search for it.**
`send.c:161` routes the failure through
`ten_extension_increment_msg_not_connected_count`, whose whole test is
`entry->count % TEN_MSG_NOT_CONNECTED_COUNT_RESET_THRESHOLD == 0`
(`msg_not_connected_cnt.c:76`, threshold `1000`). The counter starts at zero and
zero satisfies the test, so the **first** occurrence always prints; the branch
then resets the counter and the next 999 are silent. The counter is keyed per
extension *and* per message name.

The consequence is that the absence of this line proves nothing. A message
dropped a few hundred times an hour may have logged exactly once, hours ago —
so grep the whole file, not its tail.

This is a real case: the ASR extension emits an `error` data message, but
`websocket-example`'s graph routes only `asr_result`, `metrics` and
`tts_flush_end`. A missing API key therefore never reaches the UI and lives only
in the log. **When the interface shows nothing, grep for that line first.**

## 4. RPC: cmd and CmdResult

Source: `core/src/ten_runtime/binding/python/interface/ten_runtime/async_ten_env.py`

`cmd` is the framework's RPC. Python offers two shapes.

**Single reply**

```python
result, err = await ten_env.send_cmd(cmd)
# the runtime guarantees result.is_completed()
```

**Streamed replies**

```python
async for result, err in ten_env.send_cmd_ex(cmd):
    if err: break
    ...
    if result.is_completed(): break
```

`send_cmd_ex` backs onto a queue of `maxsize=10`, so intermediate results
arrive as they are produced and the last one carries `is_completed()`. Token-by-
token LLM output travels this way.

**Replying**

```python
await ten_env.return_result(CmdResult.create(StatusCode.OK, cmd))
```

**One-way sends**

```python
await ten_env.send_data(data)
await ten_env.send_audio_frame(frame)
await ten_env.send_video_frame(frame)
```

Each accepts an optional `SendOptions`; the default is fire-and-forget, while
`wait_for_result=True` waits for the runtime to confirm delivery — which reports
whether it was delivered, not what the receiver did with it.

Two mistakes that cost the most time:

- `ten_env.get_property_*()` returns a **`(value, error)` tuple**, not the
  value. Always take `[0]`.
- Extensions run off the main thread, so **no signal handlers and no `atexit`**.
  Clean up in `on_stop()`.

## 5. How the runtime loads an addon

Source: `core/src/ten_runtime/addon/addon_autoload.c`

An addon package is a directory. The runtime scans its `lib/` subdirectory and
**`dlopen`s every `.so` it finds there**:

```c
// addon_autoload.c:441
// Load the library from the 'lib/' directory.
success = load_all_dynamic_libraries_under_path(
    ten_string_get_raw_str(&addon_lib_folder_path));
```

```
tenapp/ten_packages/
├── system/
│   ├── ten_runtime/lib/          libten_runtime.so, libten_utils.so
│   ├── ten_runtime_python/lib/   libten_runtime_python.so
│   └── ten_runtime_go/lib/       libten_runtime_go.so
├── addon_loader/
│   └── python_addon_loader/lib/  libpython_addon_loader.so
└── extension/
    ├── agora_rtc/lib/            libagora_rtc.so
    └── soniox_asr_python/        pure Python — no lib/ at all
```

An extension with native code is therefore two JSON files plus `lib/*.so`; a
pure-Python one has no `lib/` and reaches the runtime through
`python_addon_loader`.

**This is why an architecture mismatch removes an extension entirely.**
`dlopen` of an x86-64 object in an aarch64 process fails, and a failed load is
not a degraded extension — it is an absent one.

---

# Part II — Platform

## 6. The ABI floor

Two properties of the published arm64 binaries decide whether they will load at
all. Both were read with `readelf` against
`ten_packages-linux-arm64-gcc-release.zip` at 0.11.71.

### 6.1 glibc ≥ 2.38

| Library | Highest glibc symbol required |
| ------- | ----------------------------- |
| `libten_utils.so` | `__isoc23_strtol@GLIBC_2.38` |
| `libpython_addon_loader.so` | `__isoc23_strtol@GLIBC_2.38` |
| `libten_runtime.so` | `GLIBC_2.34` |
| `libten_runtime_python.so` | `GLIBC_2.17` |

The x64 packages top out at 2.34 for the same libraries. The difference is not a
deliberate choice: `__isoc23_strtol` is what glibc's headers redirect `strtol`
to on 2.38+ with a recent GCC. It is an artefact of `linux_arm64.yml` running on
`ubuntu-24.04-arm` while `linux_ubuntu2204.yml` runs on 22.04, and it raises the
arm64 floor by four glibc releases.

### 6.2 The Python version must match the binding

`ten_runtime_python` is compiled against one Python's headers, and
`python_addon_loader` `dlopen`s libpython by a hardcoded filename:

| | x64 packages | arm64 packages |
| --- | --- | --- |
| `ten_runtime_python` built against | Python 3.10 headers | **Python 3.12 headers** |
| `python_addon_loader` default dlopen target | `libpython3.10.so` | `libpython3.10.so` |

The arm64 pair is internally inconsistent — the binding wants 3.12, the loader
looks for 3.10 — so the loader has to be pointed at the Python the binding was
actually built against:

```bash
export TEN_PYTHON_LIB_PATH=/usr/lib/aarch64-linux-gnu/libpython3.12.so   # Debian layout
export TEN_PYTHON_LIB_PATH=/usr/lib64/libpython3.12.so                   # RPM layout
```

Without it: `[Python addon loader] Failed to load system libpython.`

`tman check env` still reports *"TEN Framework only supports Python 3.10"* and
suggests `pyenv install 3.10.18`. On arm64 that advice is wrong for the
published packages — following it gives a 3.10 interpreter against a 3.12 binding.

### 6.3 Distribution compatibility

| Distribution | glibc | System Python | Prebuilt arm64 packages |
| ------------ | ----- | ------------- | ----------------------- |
| Ubuntu 24.04 LTS | 2.39 | 3.12 | **Works** — set `TEN_PYTHON_LIB_PATH` |
| Ubuntu 24.10 / 25.04 | 2.40+ | 3.12+ | Works if libpython matches 3.12 |
| Debian 13 (trixie) | 2.41 | 3.13 | Needs a 3.12 libpython installed |
| Fedora 39 / 40 | 2.38 / 2.39 | 3.12 | **Works** — libpython is under `/usr/lib64` |
| Fedora 41+ | 2.40+ | 3.13 | **Works** — needs `python3.12` alongside |
| Lychee 2025 (Fedora rebuild) | 2.41 | 3.13 | **Verified** — the platform Part III's RPM chapter was written on |
| Ubuntu 22.04 LTS | 2.35 | 3.10 | **No** — below the floor |
| Debian 12 (bookworm) | 2.36 | 3.11 | **No** |
| RHEL / Rocky 9 | 2.34 | 3.9 | **No** |
| Amazon Linux 2023 | 2.34 | 3.9 | **No** — relevant for Graviton |
| Alpine (musl) | — | — | **No** — no musl builds exist |

Below the floor, do not fight the prebuilt packages. Build the core from source
instead: the result links against your own glibc and your own Python, which
sidesteps both constraints.

## 7. Which shared object does what

Four questions per row: what it provides, what produces it, what loads or links
it, and whether an aarch64 build exists.

| `.so` | Provides | Produced by | Loaded / linked by | aarch64 |
| ----- | -------- | ----------- | ------------------ | ------- |
| `libten_utils.so` | strings, containers, logging, backtrace, atomics, `${env:}` resolution | `core/src/ten_utils/BUILD.gn`, `shared_library("ten_utils_shared")` | linked by `libten_runtime.so` | verified |
| `libten_runtime.so` | the C runtime: graph parsing, extension lifecycle, **message routing**, addon loading | `core/src/ten_runtime/BUILD.gn`, `output_name = "ten_runtime"` | loaded by the worker; linked by every binding | verified |
| `libten_runtime_python.so` | the Python binding | `binding/python/native/BUILD.gn`, `ten_shared_library` | loaded on `import ten_runtime` | verified |
| `libpython_addon_loader.so` | starts the Python interpreter in the worker, `dlopen`s libpython | `packages/core_addon_loaders/python_addon_loader/BUILD.gn` | `dlopen`ed by the runtime from `addon_loader/*/lib/` | verified |
| `libten_runtime_go.so` | the Go binding | `binding/go/BUILD.gn`, `ten_package` | linked by the Go app through CGO | verified |
| `libten_runtime_nodejs.so` | the Node.js binding | `binding/nodejs/native/BUILD.gn` | `nodejs_addon_loader` | build system supports it |
| `libagora_rtc.so` | **RTC transport** — implements TEN's extension interface over Agora's SDK | not in this repo; Agora supplies the source | `dlopen`ed from `extension/agora_rtc/lib/` | build it — Part III ch. 12 |
| `libagora_rtc_sdk.so` and four codec libraries | Agora's SDK | Agora | linked by `libagora_rtc.so` | available; repackage it |

### 7.1 The only prebuilt

One binary dependency in the whole build is not compiled from source:

```
third_party/node/BUILD.gn
  ten_prebuilt_library("prebuilt_node_shared")
    node-shared-linux-arm64-gcc.zip / node-shared-linux-arm64-clang.zip
```

**It has arm64.** Every other `third_party` entry — curl, zlib, mbedtls, msgpack,
libuv, libwebsockets, yyjson, nlohmann_json, googletest, ffmpeg — is built from
source, so nothing else needs a per-architecture artefact.

## 8. Component inventory and what arm64 changes

`BUILD.gn` defines one group, `ten_framework_all`, and everything hangs off it:

| Target | Contents |
| ------ | -------- |
| `core/src/ten_runtime` | the C runtime |
| `core/src/ten_runtime/binding` | the go, nodejs and python bindings |
| `core/src/ten_rust` | Rust crates, gated on `ten_enable_ten_rust` |
| `core/src/ten_manager` | `tman`, gated on `ten_enable_ten_manager` |
| `third_party` | fourteen dependencies, all source |
| `packages/core_addon_loaders` | `python_addon_loader`, `nodejs_addon_loader` |
| `packages/core_apps` | `default_app_{cpp,go,nodejs,python}` |
| `packages/core_extensions` | nine `default_*` templates |
| `packages/core_protocols` | `msgpack` |
| `packages/core_systems` | `pytest_ten` |
| `packages/example_apps` | `pprof_app_go`, `transcriber_demo` |
| `packages/example_extensions` | eighteen, including the ffmpeg trio, `vosk_asr_cpp`, `webrtc_vad_cpp` |

Twenty-two feature flags govern it: seven in `build/options.gni`, fifteen in
`build/ten_runtime/options.gni`.

### 8.1 What is actually unavailable or disabled on arm64

Only the first is a functional gap. The rest are listed so they are not mistaken
for one.

| Item | Nature |
| ---- | ------ |
| The `agora_rtc` extension | The one gap in the registry. Buildable from source — Part III ch. 12. |
| Coverage instrumentation | Cannot be enabled at all. Six `assert(is_linux && target_cpu == "x64")` guard it — three in `build/ten_runtime/glob.gni`, two in `build/ten_runtime/ten.gni`, one in `build/ten_common/rust/rust.gni`. All sit inside `if (enable_coverage)`, so a default build never reaches them. |
| Tests | `linux_arm64.yml` sets `ten_enable_tests=false` with the rust and manager test flags, so the arm64 job builds no test target. |
| `ten_enable_libwebsockets=false` | Costs nothing. The flag is referenced in exactly three places, all under `tests/ten_runtime/`, and arm64 CI disables tests anyway. It gates no runtime code. |
| `ten_manager_enable_frontend=false` | The tman designer's frontend is not built. |
| `ten_enable_go_app_leak_check` | Defined x64-only, and only meaningful under a sanitizer debug build. |
| `rustup target add stable x86_64-unknown-linux-gnuasan` | Hardcoded in the build Dockerfile, meaningless on arm64. |
| ffmpeg extensions | Off by default everywhere (`ten_enable_ffmpeg_extensions = false`), not an arm64 restriction. |

The fourth row corrects a plausible misreading: turning `libwebsockets` off looks
like dropping a transport, and is not.

## 9. Build dependencies

`tools/docker_for_building/ubuntu/22.04/Dockerfile` is the only written record of
what the core build needs, and it is Ubuntu 22.04 and Debian tooling throughout.
On any other distribution it must be read as a manifest rather than run.

| Purpose | Packages (Ubuntu names) |
| ------- | ----------------------- |
| Compiler and build | `build-essential` `cmake` `make` `autoconf` `libtool` `pkg-config` |
| Crypto and network | `libssl-dev` `libcurl4-gnutls-dev` `libcrypto++-dev` `libnss3-dev` |
| Parsing | `libexpat1-dev` `libmsgpack-dev` `zlib1g-dev` |
| System | `uuid-dev` `libunwind-dev` `libffi-dev` `libreadline-dev` `libncurses5-dev` `libgdbm-dev` |
| Audio | `libasound2` |
| ffmpeg extensions only | `libavformat-dev` `libavfilter-dev` `libx264-dev` `libdrm-dev` `libxcomposite-dev` `libxdamage1` |
| Sanitizer | `libasan5` |
| Python | `python3` `python3-dev` `python3-pip` `python3-venv` |
| Toolchains | Go 1.22.3 plus go1.20.12; Rust stable with `cbindgen`; clang-18 from apt.llvm.org |
| Tools | `uv` `task` `jq` `git` `zip` `unzip` `p7zip-full` `tree` `cpulimit` `iwyu` |

### 9.1 What the build actually links

GN's `libs` declarations across `core/`, `packages/` and `build/` resolve to
libc-level libraries only:

```
stdc++  c++  pthread  gcc  gcc_s  rt  m  dl  c        (bcrypt on Windows)
```

No `ssl`, `crypto`, `curl`, `z`, `expat`, `uuid`, `unwind`, `asound` or
`msgpack`. Several entries in the table above have **no consumer** in the GN
graph, checked by grep over `core/`, `packages/` and `build/`:

| Package | Finding |
| ------- | ------- |
| `libmysqlclient-dev`, `libmysqlcppconn-dev` | zero references |
| `iwyu`, `cpulimit`, `p7zip-full/rar` | zero references |
| `libunwind-dev` | the hits are `#include "unwind.h"` and `_Unwind_Backtrace` — **libgcc's unwinder**, not libunwind |
| `libssl-dev` | the hit is the word "endle**ssl**y" in a comment |
| `libffi-dev` | the hit is "e**ffi**ciently" in a comment |
| `uuid-dev` | TEN implements its own `ten_uuid`; no `#include <uuid/uuid.h>` |
| `libexpat1-dev`, `libnss3-dev`, `libreadline-dev`, `libncurses5-dev`, `libgdbm-dev`, `libasound2` | zero references |

One entry is genuinely required, conditionally:

| Package | Needed by | When |
| ------- | --------- | ---- |
| `libx264-dev` | `third_party/ffmpeg`'s autotool line carries `--enable-libx264` | only with `ten_enable_ffmpeg_extensions=true` |

The source-built dependencies need no system counterpart. `curl` is configured
with `CURL_ENABLE_SSL=ON` **and** `CURL_USE_MBEDTLS=ON`, so it uses the
source-built mbedtls and OpenSSL is not involved.

### 9.2 Three lines that do not survive a move off Ubuntu x64

```dockerfile
ln -sf /usr/bin/python3.10-config /usr/bin/python3-config   # hardcodes 3.10
rustup target add stable x86_64-unknown-linux-gnuasan       # hardcodes x86_64
add-apt-repository "deb http://apt.llvm.org/..."            # Debian-only, for clang-18
```

The Go install is the counter-example worth copying: it derives the archive name
from `dpkg --print-architecture`, so it is already arch-correct — though `dpkg`
itself has to be replaced on an RPM distribution.

---

# Part III — Building

## 10. The framework core from source

Needed when the target is below the ABI floor, when the C runtime itself has to
change, when the core tests must run, or to build a C++ extension.

`core/ten_gn` is a submodule and **is not populated on a fresh clone** — nothing
builds until it is initialised, and `tgn` comes from there:

```bash
git submodule update --init --recursive --depth 1 core/ten_gn
export PATH=$(pwd)/core/ten_gn:$PATH
```

It ships aarch64 `gn` and `ninja` binaries, so the build tools themselves need no
compiling. `build.py:87` selects them from `platform.machine()`, and
`validate_cpu` accepts `linux/arm64`, so no flag is required on an arm64 host.

Toolchain: clang or gcc, cmake, Go ≥ 1.20, Rust stable with `cbindgen`, Python 3
with `python-dotenv` and `jinja2`, Node only for the tman designer frontend.
Chapter 9 is the dependency list.

The root `Taskfile.yml` detects the host architecture, so on arm64 this targets
arm64 with no extra flags:

```bash
task gen
task build
# output: out/linux/arm64/
```

To match CI exactly (`.github/workflows/linux_arm64.yml`, gcc + release):

```bash
tgn gen linux arm64 release -- \
  is_clang=false log_level=1 enable_serialized_actions=true \
  ten_enable_tests=false ten_rust_enable_tests=false ten_manager_enable_tests=false \
  ten_enable_libwebsockets=false ten_enable_cargo_clean=true \
  ten_enable_rust_incremental_build=false ten_manager_enable_frontend=false
tgn build linux arm64 release
```

Two differences from x64: arm64 CI does not enable `ten_enable_ffmpeg_extensions`
and turns `ten_enable_libwebsockets` off. Re-enable either only with time
budgeted to debug it. Coverage cannot be enabled at all — see 8.1.

**Cross-compiling is not set up.** `toolchain/clang_cross/` contains only `win`;
Linux targets have `platform/linux:clang` and `platform/linux:gcc`, both native.
A cross build would need `is_clang=true` (only that branch adds
`--target=aarch64-linux-gnu`) plus a complete aarch64 sysroot via `AG_SYSROOT`,
and it would no longer be the configuration Agora and CI use. Build on an arm64
host instead.

## 11. AI agents on Ubuntu 24.04

The path of least resistance, and what CI uses.

```bash
# 1. System packages
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential cmake pkg-config git curl unzip jq \
  python3 python3-dev python3-pip python3-venv \
  libasound2t64 libunwind-dev libssl-dev libc++1 libgstreamer1.0-dev

# 2. The Python ABI redirect — see 6.2
export TEN_PYTHON_LIB_PATH=/usr/lib/aarch64-linux-gnu/libpython3.12.so
test -e "$TEN_PYTHON_LIB_PATH"

# 3. Go, Node, Bun
curl -fsSLO https://go.dev/dl/go1.24.3.linux-arm64.tar.gz
sudo tar -C /usr/local -xzf go1.24.3.linux-arm64.tar.gz
export PATH=/usr/local/go/bin:$PATH
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs
curl -fsSL https://bun.sh/install | bash && export PATH="$HOME/.bun/bin:$PATH"

# 4. uv, task, tman — all have arm64 builds
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
sudo sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b /usr/local/bin
curl -fsSL -o /tmp/tman.zip \
  https://github.com/TEN-framework/ten-framework/releases/download/0.11.71/tman-linux-release-arm64.zip
unzip -q /tmp/tman.zip -d /tmp/tman
sudo install -m 0755 /tmp/tman/ten_manager/bin/tman /usr/local/bin/tman

# 5. Confirm before building anything
tman check env
```

Then an example:

```bash
cd ai_agents
cp .env.example .env      # fill in provider keys
cd agents/examples/websocket-example
task install
task run
```

`task install` rewrites `tenapp/manifest-lock.json` with arm64 entries. That is
expected — see 14.3.

### 11.1 Python dependencies

Every extension in `ai_agents/agents/ten_packages/extension/` is pure Python with
no bundled binaries, so nothing there needs compiling for arm64. The aggregate
set includes `numpy`, `scipy`, `torch`, `pillow`, `cryptography` and `soundfile`,
all of which publish `manylinux` aarch64 wheels.

`install_python_deps` installs requirements for **every** extension in the tenapp,
not just the ones a graph uses.

## 12. AI agents on RPM-based distributions

Verified end to end on an aarch64 Fedora rebuild — Lychee 2025, glibc 2.41,
gcc 14.3.1, dnf5 — as an ordinary non-root user, with no container anywhere.
Everything below applies to stock Fedora, which shares the layout the differences
are about. One item is specific to a rebuild and is marked as such.

### 12.1 What carried over unchanged

The parts that looked riskiest turned out to be portable. `tman install` resolved
the arm64 packages straight from the registry. The Go binding linked against the
arm64 `libten_runtime.so` and built clean (`GOARCH=arm64`, `GOARM64=v8.0`). And
every Python dependency resolved to an aarch64 wheel — `pydantic_core`,
`aiohttp`, `awscrt` and the rest arrived as `manylinux_*_aarch64`, with nothing
falling back to a source build.

### 12.2 libpython lives in /usr/lib64

RPM distributions do not use Debian's multiarch layout:

```bash
export TEN_PYTHON_LIB_PATH=/usr/lib64/libpython3.12.so
```

The symlink comes from `python3.12-devel`; `python3.12-libs` supplies the
`libpython3.12.so.1.0` it points at.

### 12.3 Fedora 41+ ships Python 3.13, not 3.12

Ubuntu 24.04 happens to ship exactly the 3.12 the arm64 binding was compiled
against, which is why the Ubuntu path needs no version work. Fedora 41 and later
default to 3.13, so 3.12 has to be installed alongside:

```bash
sudo dnf -y install python3.12 python3.12-devel
```

Parallel Python versions are installable, so this leaves the system interpreter
alone.

### 12.4 UV_PYTHON must be pinned

The failure with the least helpful symptom, and it does not exist on Ubuntu.
Both dependency installers hardcode `uv pip install --system` —
`install_python_deps.py` in the tenapp, and `PIP_INSTALL_CMD` in
`install_deps_and_build.sh` — and `--system` selects the *default* interpreter,
3.13 here. The dependencies land in 3.13's site-packages while the runtime loads
3.12 through `TEN_PYTHON_LIB_PATH`. Nothing fails at install time; it surfaces
much later as `ModuleNotFoundError` inside an extension.

```bash
export UV_PYTHON=/usr/bin/python3.12
```

### 12.5 `uv pip install --system` needs root

`--system` writes to `/usr/local/lib/python3.12/site-packages` and its `lib64`
sibling. Inside the container this is invisible because the container runs as
root; on bare metal as an ordinary user, `task install` fails three retries deep
on every extension with `Permission denied (os error 13)`.

Elevate only that step. Do **not** `sudo task install` — that runs `bun install`
and `go build` as root too, leaving root-owned artefacts in the user's home:

```bash
cd ai_agents/agents/examples/<example>/tenapp
sudo env "PATH=$PATH" UV_PYTHON=/usr/bin/python3.12 python3 scripts/install_python_deps.py
```

`task install` aborts at that step, so the two after it have to be finished by
hand — or use `finish_example_install_arm64.sh` (chapter 17).

Both `/usr/local/lib/python3.12/site-packages` and its `lib64` sibling are on
`/usr/bin/python3.12`'s default `sys.path`, so the embedded interpreter finds
what lands there.

### 12.6 Package names

| Ubuntu | Fedora |
| ------ | ------ |
| `build-essential` | `dnf group install "Development Tools"` |
| `python3-dev` | `python3.12-devel` |
| `libasound2t64` | `alsa-lib-devel` |
| `libunwind-dev` | `libunwind-devel` |
| `libssl-dev` | `openssl-devel` |
| `libc++1` | `libcxx` |
| `libgstreamer1.0-dev` | `gstreamer1-devel` |
| `pkg-config` | `pkgconf-pkg-config` |

### 12.7 Installers keyed on os-release (rebuilds only)

Does not apply to stock Fedora. `https://rpm.nodesource.com/setup_20.x` exits
with `Error: This script is intended for RPM-based systems.` on a rebuild whose
`/etc/os-release` carries its own `ID` and leaves `ID_LIKE` unset — the script
enumerates distributions by that field and never checks whether `rpm` is
present. Lychee 2025 reports `ID=lychee` with no `ID_LIKE`, so it is refused
despite being an RPM distribution with `dnf5`.

Use the distribution's own package; the playground runs under `bun` rather than
Node, so the Node major version is not load-bearing:

```bash
sudo dnf -y install nodejs npm
```

### 12.8 Reading `tman check env` here

Two findings are expected and neither blocks an agents build:

| Report | Reality |
| ------ | ------- |
| `⚠️ python3 3.13.9 … only supports Python 3.10` | It inspects `python3`, the system 3.13. The interpreter that runs extensions is the 3.12 behind `TEN_PYTHON_LIB_PATH`. Taking its `pyenv install 3.10.18` advice breaks the build. |
| `❌ tgn Not installed` | `tgn` is only needed to build the core or a C++ extension. A Python example's `task install` runs `tman install`, `install_python_deps.py`, `bun install` and `go build`, none of which invoke it. |

### 12.9 Firewall and SELinux

Fedora runs firewalld by default. Browsing from the machine itself is
unaffected; reaching the services from elsewhere needs the ports opened:

```bash
sudo firewall-cmd --add-port=3000/tcp --add-port=8080/tcp --add-port=49483/tcp
```

SELinux is relevant only to the container path. Under enforcing, the five bind
mounts in `docker-compose.yml` carry no `:z` label and the container cannot read
`/app`; add the labels through a local `docker-compose.override.yml` rather than
editing the tracked compose file. A host in permissive mode is unaffected.

### 12.10 Verification result

Verified with `verify_arm64_install.sh --probe-worker`: **31 passed, 0 failed,
2 skipped.**

Read that as a floor, not a verdict. It says one example installs and starts
correctly — a single graph, a single throwaway session, no conversation. The
scope block the script prints lists what it leaves alone.

Within that scope, the two findings that matter are dynamic; no static check
reaches them:

```
8b. Worker probe
  POST /start accepted
  worker for verify-<pid> is registered

9. Worker logs
  no libpython load failure in worker logs
  no ModuleNotFoundError in worker logs
```

A clean worker log settles the architecture question specifically: it means
`python_addon_loader` resolved `libpython3.12` through `TEN_PYTHON_LIB_PATH`
instead of its built-in `libpython3.10.so`, and that the dependencies `uv`
installed are visible to the interpreter the runtime embedded. It says nothing
about whether any particular graph does useful work.

## 13. AI agents in a container

The default image `ghcr.io/ten-framework/ten_agent_build:0.7.14` is a single-arch
`linux/amd64` manifest, so `docker-compose.yml` pins `platform: linux/amd64` and
runs under emulation on arm64.

`ai_agents/Dockerfile.dev` builds an arm64 replacement on `ubuntu:24.04`:

```bash
cd ai_agents
docker build -f Dockerfile.dev -t ten_agent_dev:local .
```

Then set both variables in `ai_agents/.env`:

```
TEN_AGENT_DEV_IMAGE=ten_agent_dev:local
TEN_AGENT_DEV_PLATFORM=linux/arm64
```

It is deliberately arm64-only and fails the build on amd64. A single image cannot
serve both: x64 packages need Python 3.10 (Ubuntu 22.04) while arm64 packages
need glibc 2.38 (Ubuntu 24.04, Python 3.12), and those are mutually exclusive on
one base.

Do not run the amd64 image on arm64. It trips the AVX2 probe in
`agents/scripts/install_deps_and_build.sh`, which exists to catch exactly that,
and aborts with `FATAL: unsupported platform.`

The container removes three of the four RPM-specific problems, because the
container is Ubuntu regardless of the host. It does not remove the fourth
(SELinux bind-mount labels), and it answers nothing about whether the framework
runs natively on the host distribution.

## 14. Building agora_rtc for aarch64

The published `agora_rtc` extension is a linux/x64 package, and that is what
blocks 24 of the 26 examples on arm64. The extension is not open source, but
Agora supplies the source on request, and it builds for aarch64 with **no change
to its logic** — three environmental edits and the ordinary build command.

Verified end to end: the built extension connects to Agora, publishes an audio
track and opens a data stream on an aarch64 host.

### 14.1 RTC is two packages, not one

This distinction causes more confusion than anything else here.

```
TEN runtime
    │  dlopen — the runtime scans extension/agora_rtc/lib/
    ↓
libagora_rtc.so          the wrapper: implements TEN's extension interface
    │  calls createAgoraService()
    ↓
libagora_rtc_sdk.so      Agora's SDK: a C++ library that knows nothing about TEN
    │  links
    ↓
libaosl.so, libagora-fdkaac.so, libagora-ffmpeg.so, libagora-soundtouch.so
```

| Package | What it is | aarch64 |
| ------- | ---------- | ------- |
| `agora_rtc_sdk` (system) | Agora's own SDK | Agora publishes it; repackage with `package_agora_rtc_sdk_arm64.sh` |
| `agora_rtc` (extension) | the wrapper the runtime actually `dlopen`s | build from source |

**The SDK having an aarch64 build does not help on its own.** The SDK does not
know TEN exists; the bridge is `libagora_rtc.so`, and that is the object the
runtime loads.

### 14.2 The aarch64 SDK is the right SDK

At first glance the aarch64 tarball looks like a different product: 45 of the x64
package's 109 headers are absent, including `IAgoraRtcEngine.h`,
`IAgoraRtcEngineEx.h` and `IAgoraMediaEngine.h`. The x64 package is assembled
from the full RTC SDK; the aarch64 tarball is the RTSA / server SDK.

Symbol analysis says that does not matter. The x64 `libagora_rtc.so` resolves
exactly **two** symbols from the SDK:

```
createAgoraService      the low-level / server entry point (IAgoraService.h)
getAgoraSdkVersion
```

It references no `agora::` class symbols directly — everything else goes through
vtables — and never touches `createAgoraRtcEngine` or `IRtcEngine`. Both symbols
are exported by the aarch64 build, confirmed with `nm -D`. The wrapper uses the
low-level API exclusively, which is precisely what the aarch64 tarball provides.

The tarball ships both header sets over one library, and the library's exported
ABI is C: 348 `agora_*` functions, nine `create*`/`get*` factories, and **zero**
C++ mangled symbols, vtables or typeinfo. The C++ interface still works —
`createAgoraService()` returns a pointer whose virtual calls dispatch through the
object's own vptr — which is why only two symbols need resolving.

### 14.3 The version pairing

Two versions are pinned against each other, and getting this wrong costs more
time than anything else in this chapter.

| wrapper | requires SDK |
| ------- | ------------ |
| `0.23.9-t1` — the version the examples pin | `=4.4.32-141` |
| `0.26.0-rc14` — master at the time of writing | `=4.4.32-175` |

The pin is exact for a reason. `0.26` calls `setTotalExtraSendMs` and overrides
`onCustomUserInfoUpdated`; neither exists in the 141 headers, so building it
against 141 fails at compile time. **Editing the pin does not help** — a version
number is not what is missing.

Ask Agora for the wrapper source **and** the SDK build its manifest pins, then
check the pairing before building anything:

```bash
python3 -c "import json;m=json.load(open('manifest.json'));print(m['version'],
  [d['version'] for d in m['dependencies'] if d['name']=='agora_rtc_sdk'])"
strings <sdk>/agora_sdk/libagora_rtc_sdk.so | grep -oE '4\.4\.32\.[0-9]+' | sort -u
```

`0.26` also adds two features worth knowing about, since they explain the new
API calls: an audio-track selection refactor (three send modes chosen from
`audio_scenario`, `extra_send_ms` and a fallback flag) and a fine-grained
reporting transformation. Mode #3 — Extra Audio Send — is reachable only when
`extra_send_ms != 0`, which no shipped graph sets.

### 14.4 The three edits

None of them touches the extension's behaviour.

**1. Declare arm64.** The manifest ships `supports` as x64 only, so tman will not
install the result on arm64. Add the entry, keeping x64:

```json
"supports": [
  { "os": "linux", "arch": "x64" },
  { "os": "linux", "arch": "arm64" }
]
```

**2. Drop the x86-64 object from `resources`.** `BUILD.gn` packages
`lib/liblinux_audio_hy_extension.so`, which ships as x86-64 only. The runtime
`dlopen`s every `.so` under an addon's `lib/`, so an object of the wrong
architecture fails the load rather than being skipped (see 5). Remove that line
for an arm64 build; restore it for x64.

**3. Add `#include <cstdint>`.** GCC 13 stopped including `<cstdint>`
transitively, and the source relies on it in 27 files (`0.23.9-t1`) or 29
(`0.26.0-rc14`). **This is not an arm64 issue** — the same build fails on x64
with GCC 14 — and is worth reporting upstream. To find them:

```bash
for f in $(find src -name '*.h' -o -name '*.cc'); do
  grep -qE '\b(u?int(8|16|32|64)_t|uintptr_t|intptr_t)\b' "$f" &&
  ! grep -qE '#include\s*<c?stdint\.?h?>' "$f" && echo "$f"
done
```

### 14.5 The sequence

Four scripts, documented individually in chapter 18. Each verifies its own output
and stops rather than passing a broken artefact on.

```bash
# 1. SDK tarball -> TEN system package        (runs anywhere)
TMAN=/path/to/tman PKG_VERSION=4.4.32-141 \
  ai_agents/agents/scripts/package_agora_rtc_sdk_arm64.sh <sdk.tgz> /tmp/out

# 2. build the wrapper                        (must run ON an aarch64 host)
ai_agents/agents/scripts/build_agora_rtc_arm64.sh <wrapper-src> /tmp/out/agora_rtc_sdk-*.tpkg

# 3. place it into an example
ai_agents/agents/scripts/install_agora_rtc_arm64.sh \
  <wrapper-src>/out/linux/arm64/ten_packages/extension/agora_rtc \
  /tmp/out/agora_rtc_sdk-*.tpkg voice-assistant

# 4. finish the rest of `task install`
ai_agents/agents/scripts/finish_example_install_arm64.sh voice-assistant
```

The build command differs from Agora's own Taskfile by one word:

```
theirs   tgn gen linux x64   release -- is_clang=false
arm64    tgn gen linux arm64 release -- is_clang=false
```

The wrapper links five Agora libraries plus three system ones, so
`openssl-devel` and `zlib-devel` must be present:

```
ten_runtime  ten_utils
agora_rtc_sdk  agora-fdkaac  agora-ffmpeg  agora-soundtouch  aosl
z  crypto  ssl
```

The x64 package's SDK directory holds eight libraries, three more than the
aarch64 tarball. Those three — `libagora_stt_ag_extension.so`,
`libagora_stt_ms_extension.so`, `libagora_mcc_ysd_extension.so` — do **not**
appear in the official x64 wrapper's `NEEDED` list, checked with `readelf -d`.
The aarch64 package is complete.

### 14.6 Two traps worth naming

**tman consults only the registry named `default`.** `registry.get("default")` in
`ten_manager/src/registry/mod.rs` is the whole selection logic, so a local
`file://` registry cannot coexist with the official one — pointing `default` at
it would stop `ten_runtime` and everything else from resolving. The scripts
install the rest through tman and place the SDK by hand.

**`tman install` re-resolves and rewrites the lock.** Running it on
`voice-assistant` upgraded 58 packages, `ten_runtime` among them. That is
expected — no example passes `--locked` — but it means the runtime the extension
loads against may not be the one it was compiled against. Step 6 of
`install_agora_rtc_arm64.sh` resolves symbols against the tenapp's actual
`ten_runtime` for exactly this reason.

### 14.7 Verifying the result

Loading is not the same as working. A session that reaches Agora logs this:

```
[agora_rtc] on_start() done
onConnecting:  channelId <channel>, state 2
onConnected:   localUserId <uid>, state 3, reason 1
               sid(<session id>)
custom audio_track created
audio track published
onAudioTrackPublishSuccess
```

The `sid` comes from Agora's servers, so its presence distinguishes a real
connection from local initialisation that never reached the network.

```bash
curl -s -X POST localhost:8080/start -H 'Content-Type: application/json' \
  -d '{"request_id":"t1","channel_name":"probe","graph_name":"voice_assistant","user_uid":1234}'
sleep 10 && curl -s localhost:8080/list
```

A worker in `/list` means the addon loaded. Nothing there, and the reason is in
the worker's own output — chapter 19.

---

# Part IV — Development

## 15. Writing and maintaining an extension

An extension is four files and three names that must agree.

```
addon.py / main.cc     @register_addon_as_extension("X")   name 1
manifest.json          "name": "X"                         name 2
property.json          defaults
graph node             "addon": "X"                        name 3
```

Section 3.3 covers what happens when they disagree. A node's `name` is separate
and is what `connections` routes by.

### 15.1 The manifest is the contract

```json
"api": {
  "property": { "properties": { … } },
  "cmd_in": [], "cmd_out": [],
  "data_in": [], "data_out": [],
  "audio_frame_in": [], "audio_frame_out": [],
  "video_frame_in": [], "video_frame_out": []
}
```

The schema is `core/src/ten_rust/src/json_schema/data/manifest.schema.json`. Only
three top-level fields are required — `type`, `name`, `version` — and
`additionalProperties` is `false`, so an unrecognised key is rejected outright.
Permitted top-level keys are `type`, `name`, `version`, `description`,
`display_name`, `readme`, `tags`, `dependencies`, `dev_dependencies`, `api`,
`supports`, `package`, `scripts`.

Changing `api` changes the contract. Adding a property is compatible — nothing
sets it, so the default applies. **Removing one, or changing what it means,
alters the behaviour of existing graphs without any error.**

Real extensions declare all ten arrays, empty ones included, and use `["**"]` for
`package.include`. Property types are `string`, `int32`, `bool`, `object`,
`array`.

### 15.2 A C++ extension

Skeleton at `packages/core_extensions/default_extension_cpp/`:

```cpp
#include "ten_runtime/binding/cpp/ten.h"

class my_extension_t : public ten::extension_t {
  void on_init(ten::ten_env_t&) override;
  void on_start(ten::ten_env_t&) override;
  void on_cmd(ten::ten_env_t&, std::unique_ptr<ten::cmd_t>) override;
  void on_data(ten::ten_env_t&, std::unique_ptr<ten::data_t>) override;
  void on_audio_frame(ten::ten_env_t&, std::unique_ptr<ten::audio_frame_t>) override;
  void on_stop(ten::ten_env_t&) override;
};
TEN_CPP_REGISTER_ADDON_AS_EXTENSION(my_extension, my_extension_t);
```

Emitting an audio frame:

```cpp
auto frame = ten::audio_frame_t::create("pcm_frame");
frame->set_sample_rate(16000);
frame->set_number_of_channels(1);
frame->set_bytes_per_sample(2);
frame->set_samples_per_channel(n);
frame->set_data_fmt(TEN_AUDIO_FRAME_DATA_FMT_INTERLEAVE);
frame->alloc_buf(size);
ten::buf_t buf = frame->lock_buf();
std::memcpy(buf.data(), pcm, size);
frame->unlock_buf(buf);
ten_env.send_audio_frame(std::move(frame));
```

**`ten_env` may only be touched from the extension thread.** A vendor SDK that
delivers on its own threads has to hop back through `ten::ten_env_proxy_t`:

```cpp
ten_env_proxy_ = ten::ten_env_proxy_t::create(ten_env);   // on the extension thread
…
// from an SDK thread — copy first, the SDK may reuse its buffer immediately
auto payload = std::make_shared<std::string>(static_cast<const char*>(pcm), bytes);
ten_env_proxy_->notify([payload](ten::ten_env_t &env) { … });
…
delete ten_env_proxy_;   // its destructor releases; do this before on_stop_done()
```

Getting that wrong produces crashes that look random and appear under load.

`ten_package()` forwards `sources`, `include_dirs`, `libs`, `lib_dirs`,
`ldflags`, `cflags*`, `configs`, `defines`, `enable_build`, `output_name`,
`resources` — but **not** `deps`. GN loads only `BUILD.gn` files reachable from
the root target, so an extension in a tenapp also needs a `group` in the app's
`scripts/BUILD.gn` depending on it, or it is silently never built.

### 15.3 Native extensions cost more to maintain

A pure-Python extension is one artefact for every platform. A native one must be
**built and published per architecture**:

```json
"supports": [{"os":"linux","arch":"x64"}, {"os":"linux","arch":"arm64"}]
```

Miss one and `dlopen` fails there — the extension does not exist on that
platform, rather than running with less. That asymmetry is the whole reason
chapter 14 exists.

Write native code when wrapping an existing C/C++ library, or when the GIL
genuinely blocks an audio path. The 89 vendor extensions are Python because they
speak HTTP and WebSocket, where it buys nothing.

## 16. Version management

Versions are declared in four places:

```
①  tenapp/manifest.json
      "dependencies": [{ "name": "agora_rtc", "version": "=0.23.9-t1" }]

②  <extension>/manifest.json
      "version": "0.26.0-rc14"
      "dependencies": [{ "name": "agora_rtc_sdk", "version": "=4.4.32-175" }]

③  <system package>/manifest.json
      "version": "4.4.32-141"
      "supports": [{ "os": "linux", "arch": "arm64" }]

④  tenapp/manifest-lock.json          what tman resolved
```

| Syntax | Meaning |
| ------ | ------- |
| `"=0.23.9-t1"` | exact — only this build |
| `"0.11"` | a range — the newest compatible |

An exact pin on a native SDK is doing real work: without it tman installs an SDK
whose API does not match and the failure moves from compile time to run time.

### 16.1 Upgrading

There is no `tman upgrade`. `tman install` **re-resolves and rewrites the lock**
unless `--locked` is passed, so:

- version pinned exactly → edit `manifest.json`, then `tman install`
- version given as a range → `tman install` alone picks up newer builds
- one package only → `tman install extension <name>@<version>`

Always check what moved:

```bash
git diff tenapp/manifest-lock.json
```

### 16.2 Manually placed packages are outside all of this

An extension placed by hand — as `agora_rtc` is on arm64 — has no
`.ten/package_info/`, so tman does not know it exists. `tman install` will not
touch it and `tman uninstall` will not find it. Upgrading means rebuilding and
re-copying: new source, matching SDK, re-apply the three edits, rebuild, place.

That is the price of stepping outside the package manager, and the reason 14.6
recommends getting the packages published rather than living here.

### 16.3 Lockfiles say less than they appear to

A committed `manifest-lock.json` records the platform resolved on the machine
that generated it, not the package's platform matrix. Every example lock in this
repo reads `{"os": "linux", "arch": "x64"}`, which looks like a hard arm64 block
and is not — the same versions are published for arm64.

Nothing here passes `tman install --locked`, so the lock is re-resolved and
rewritten against the host on every install. **Read the registry, not the lock.**

## 17. Configuration: swapping a vendor, pointing at a local model

### 17.1 Where the settings live

A vendor's settings go in the node's `property.params`, flat. There is no wrapper
naming the module or the vendor — nothing in this repo reads a
`{"asr": {"vendor": …}}` shape. Each extension's own test configs are the
authority; `soniox_asr_python/tests/configs/property_en.json` is representative:

```json
{
    "params": {
        "api_key": "${env:SONIOX_ASR_API_KEY}",
        "url": "wss://stt-rt.soniox.com/transcribe-websocket",
        "model": "stt-rt-v4",
        "language_hints": ["en"],
        "sample_rate": 16000
    }
}
```

Swapping a provider means changing `addon` and replacing `params` on that node.
Leave `name` and `extension_group` alone.

### 17.2 `${env:VAR}` has two failure modes, and they look nothing alike

The runtime resolves placeholders in
`core/src/ten_utils/lib/sys/general/placeholder.c`, recursively through objects
and arrays (`ten_extension_property_resolve_placeholders`,
`core/src/ten_runtime/extension/internal/metadata.c`), so depth does not matter.
What matters is whether the variable exists:

| State | `getenv` | Result |
| ----- | -------- | ------ |
| Not set, no `\|` default | `NULL` | `exit(EXIT_FAILURE)` — the worker dies during property resolution |
| Set but empty | `""` | resolves to an empty string; the extension rejects it later in its own words |
| Set, with a value | the value | normal |

The `|` in a placeholder supplies a default and makes the variable optional.
`"${env:OPENAI_PROXY_URL|}"` tolerates absence; `"${env:DEEPGRAM_API_KEY}"` does
not. The asymmetry is deliberate: a required key that is simply missing should
stop the process rather than let it run misconfigured.

**The empty case wastes the most time.** Copying `.env.example` gives every key
an empty value, so an unfilled key is *present* rather than absent — the middle
row, not the first. The worker starts normally and the only symptom is a vendor
error deep in the session log.

### 17.3 Nothing warns about a nested key

`ai_agents/server/internal/http_server.go` validates `${env:…}` before spawning a
worker, but the loop reads only a node's **top-level** properties and skips any
value that is not a string. Every vendor key in these examples lives under
`params`, which is an object, so it is never examined. A missing or empty vendor
key produces no `Environment variable not found` line anywhere.

`verify_arm64_install.sh` covers that gap — run it before starting a session
rather than diagnosing from logs afterwards.

### 17.4 A local LLM usually needs no code

`openai_llm2_python`'s first property is `base_url`:

```json
{ "base_url": "https://api.openai.com/v1", "api_key": "${env:OPENAI_API_KEY}", … }
```

Any local server exposing an OpenAI-compatible `/v1/chat/completions` — vLLM,
Ollama's `/v1` layer, llama.cpp's `llama-server`, LM Studio, Xinference — is a
one-field change:

```json
"property": {
  "base_url": "http://127.0.0.1:11434/v1",
  "api_key": "not-needed",
  "model": "qwen2.5:14b"
}
```

`api_key` cannot be omitted or empty: most local servers ignore its value, but an
empty string makes the extension reject it.

Write a new extension only when the protocol is genuinely incompatible. The
skeleton is `packages/core_extensions/default_llm_extension_python/` — 28 lines,
of which the only required method is `on_call_chat_completion` returning an
`AsyncGenerator[LLMResponse, None]`. The base class handles message routing, the
tool protocol and the exchange with `main_control`.

### 17.5 When a change takes effect

| Changed | Effect |
| ------- | ------ |
| a value inside `params` (voice id, model, language) | next session — the server re-reads `property.json` per worker |
| `addon`, the node set, or `connections` | full restart of `task run` |
| `.env` | full restart — read once at startup |

## 18. Testing

Two harnesses with the same shape and opposite requirements. The distinction that
matters is whether the vendor client is real.

| | Standalone | Guarder |
| --- | ---------- | ------- |
| Command | `task test-extension EXTENSION=agents/ten_packages/extension/<ext>` | `task asr-guarder-test EXTENSION=<ext>` / `tts-guarder-test` |
| Lives in | `<ext>/tests/` | `agents/integration_tests/{asr,tts}_guarder/` |
| Vendor client | **mocked** — the client class is patched out | **real** |
| Credentials | none | the vendor key |
| Network | none | yes |
| arm64 | **runs today** — depends only on runtime libraries that have aarch64 builds | architecture-correct, but never exercised there |

Both run a real TEN app rather than a stub: each `conftest.py` starts a `FakeApp`
subclass of `App` on its own thread and blocks the fixture until `on_init` fires,
so the C runtime, the Python binding and the addon manager are live throughout. A
standalone test is a unit test of the extension, not of a mock.

### 18.1 How a standalone run works

`<ext>/tests/bin/start`:

```bash
export PYTHONPATH=.ten/app:.ten/app/ten_packages/system/ten_runtime_python/lib:…
export TEN_APP_BASE_DIR=.ten/app
pytest -s tests/ "$@"
```

`.ten/app` is a throwaway app tree from `tman -y install --standalone`, which
`task test-extension` creates and deletes. `test-extension-no-install` skips
both — the faster loop while iterating, and the reason a stale `.ten/` causes
confusing failures later (`rm -rf <ext>/.ten`).

The mock patches the vendor client where the extension imports it. For
`soniox_asr_python` that is
`soniox_asr_python.extension.SonioxWebsocketClient`, replaced by a `MagicMock`
whose `connect`, `send_audio`, `finalize` and `stop` are `AsyncMock`s, plus
`trigger_open`, `trigger_transcript`, `trigger_error`, `trigger_close` and
`trigger_finished` helpers the test calls to drive the extension through states
the real vendor would produce. Sixteen files exercise finalize modes,
reconnection, confidence, multilingual output, sentence termination, invalid
params and vendor errors — none needing a key.

Anything after `--` goes to pytest:

```bash
task test-extension-no-install EXTENSION=agents/ten_packages/extension/soniox_asr_python -- -k test_finalize -s -v
```

### 18.2 How a guarder run works

One harness parameterised by extension name. It rewrites its own manifest first:

```bash
sed "s/{{extension_name}}/$EXT_NAME/g" manifest-tmpl.json > manifest.json
./scripts/install_deps_and_build.sh <os> <arch>
./tests/bin/start --extension_name <ext> --config_dir <ext>/tests/configs
```

`install_deps_and_build.sh` detects the host architecture when called without
arguments, and the Taskfile passes it explicitly, so this path is arch-correct on
arm64 — though CI has never exercised it there.

`--config_dir` points at the extension's own `tests/configs/`, the authoritative
property shape for that vendor. Those files carry real `${env:…}` placeholders,
so the corresponding key must be set.

Audio fixtures are 16 kHz PCM under `tests/test_data/`: `16k_en_us.pcm`,
`16k_en_us_helloworld.pcm`, `16k_zh_cn.pcm`, `16k_es_es.pcm`.

`tests/bin/start` excludes `test_long_duration_stream` by default. Do not run the
ASR and TTS guarders concurrently in one container — their build scripts collide
on shared temp paths.

### 18.3 Core tests

The arm64 job sets `ten_enable_tests=false` along with the rust and manager test
flags, so **no test target has ever been built on arm64**. Building them means
building the core from source (chapter 10); the binaries land in
`out/linux/arm64/tests/`. Coverage cannot be enabled — see 8.1.

### 18.4 Where test logs go

Neither harness writes a file. Both `conftest.py` files configure
`{"emitter": {"type": "console", "config": {"stream": "stdout"}}}` at debug
level, so capture it if you want one:

```bash
task test-extension EXTENSION=… 2>&1 | tee /tmp/ext_test.log
```

This is a different destination from a running agent, where the API server gives
each worker its own file under `LOG_PATH`. Test runs never write there.

---

# Part V — Operations

## 19. The scripts

Five scripts under `ai_agents/agents/scripts/`. Each verifies its own output and
stops rather than passing a broken artefact to the next one.

### 19.1 `verify_arm64_install.sh`

Checks an **installed** tenapp: what `tman install` pulled from the registry plus
what `task install` built locally. It does not check a from-source core build.
Exits with the number of failed checks, so it is usable in CI.

```bash
./verify_arm64_install.sh                  # static only
./verify_arm64_install.sh --probe-worker   # also exercises the Python binding
./verify_arm64_install.sh voice-assistant  # a different example
```

| Section | Checks |
| ------- | ------ |
| 0 Host | arch, kernel, `ID`/`ID_LIKE`/`VERSION_ID`, release package, glibc, gcc, package manager, SELinux, container detection, CPU, every `python3.x`, every libpython. Asserts the glibc floor and the package family — from the rpm database when `os-release` does not say. |
| 1 Environment | `TEN_PYTHON_LIB_PATH` and `UV_PYTHON`, and derives the interpreter the runtime will embed |
| 2 Inventory | every system package and extension with its manifest version, which addons a graph instantiates, and each extension's language. An addon a graph names but that is not installed is a failure of its own. |
| 3 Architecture | the ELF architecture of **every** shared object, not a sample |
| 4 Linkage | unresolved `NEEDED` entries, separating a library absent from the tree (a failure) from one present but off the search path (expected for a `dlopen`ed addon — reports the RUNPATH and says why) |
| 5 glibc | what `libten_runtime.so` actually requires, against the host's |
| 6 Python ABI | the libpython name the loader defaults to; probes `ten_runtime` and `ten_ai_base` **separately** so a failure is attributed to the layer that broke, printing the exception verbatim |
| 7 Artefacts | the two locally built Go binaries |
| 7b Placeholders | walks the graph for `${env:…}`, resolves each against `ai_agents/.env`, separates absent-and-required from present-but-empty, and reports a disagreement between `.env` and the environment rather than picking a winner. Never prints a value. |
| 8 Services | the three ports, `/health`, `/graphs`, and **processes in `T` state** |
| 8b Worker probe | with `--probe-worker`, starts a throwaway session through the API and stops it. Needs no credentials: a worker that fails to authenticate has still loaded libpython and instantiated its extensions. |
| 9 Worker logs | the libpython load failure and `ModuleNotFoundError`; reports **skip** rather than pass when no session has run |

Two checks exist because the failures they catch are otherwise silent. Section 8
detects processes in `T` state — a backgrounded `task run` stopped by SIGTTOU
keeps its listening socket and looks healthy to a port check while accepting
nothing. Section 9 refuses to report a pass it has not earned: an unexercised
Python binding is not evidence of a working one.

The summary states its scope before the result. A green run means one example
installs and starts on this architecture — one graph, one throwaway session, no
conversation.

### 19.2 `package_agora_rtc_sdk_arm64.sh`

Converts Agora's aarch64 tarball into a TEN `agora_rtc_sdk` system package.

```bash
TMAN=/path/to/tman PKG_VERSION=4.4.32-141 \
  ./package_agora_rtc_sdk_arm64.sh <sdk.tgz> [output-dir]
```

Refuses to run unless the libraries really are aarch64, maps the flat `include/`
tree onto the `include/rtc/low_level_api/include/` layout the x64 package uses,
stamps `supports: linux/arm64`, and writes a `PROVENANCE.md` recording where the
package came from and how it differs from the published x64 one.

`PKG_VERSION` must match what the wrapper's manifest pins — see 14.3.

### 19.3 `build_agora_rtc_arm64.sh`

Builds the wrapper. **Must run on an aarch64 host**; there is no cross-compile
path (10).

```bash
./build_agora_rtc_arm64.sh <wrapper-src-dir> <sdk.tpkg>
```

| Step | Does |
| ---- | ---- |
| 1 | installs `openssl-devel` and `zlib-devel`, checks `tgn` and `tman` |
| 2 | drops the `agora_rtc_sdk` dependency for the resolve, runs `tman -y install --standalone`, restores the manifest |
| 3 | unpacks the SDK into `.ten/app`, verifies five aarch64 libraries and the header layout |
| 4 | `tgn gen linux arm64 release -- is_clang=false` |
| 5 | `tgn build`, with `NINJAFLAGS` capped — 16k lines of C++ against this SDK is memory-hungry |
| 6 | reports the ELF architecture, the `NEEDED` list and unresolved symbols |

Step 2 exists because tman resolves the whole tree or fails, and the registry has
no arm64 `agora_rtc_sdk` — see 14.6.

### 19.4 `install_agora_rtc_arm64.sh`

Places the built extension and the SDK into an example's tenapp.

```bash
./install_agora_rtc_arm64.sh <built-extension-dir> <sdk.tpkg> [example]
```

The layout it produces was read off an official x64 `tman install`, not inferred:

```
ten_packages/extension/agora_rtc/{manifest.json,property.json,lib/*.so}
ten_packages/system/agora_rtc_sdk/{include,lib}/
```

Step 5 scans every `.so` and **aborts on any x86-64 object**; step 6 resolves
symbols with `LD_LIBRARY_PATH` set the way the runtime will have it.

### 19.5 `finish_example_install_arm64.sh`

What `install_agora_rtc_arm64.sh` deliberately leaves out — the rest of
`task install`.

```bash
./finish_example_install_arm64.sh [example]
```

| Step | Does |
| ---- | ---- |
| 2 | builds `tenapp/bin/main`, which `scripts/start.sh` execs. Without it `tman run start` fails with exit 127 |
| 3 | installs each extension's `requirements.txt`, elevated — and only this step |
| 4 | `bun install` in the **shared** `ai_agents/playground`, which is what `voice-assistant` runs |
| 5 | resolves `bin/main`'s libraries the way `start.sh` will, and imports the core third-party packages |

The two halves of `scripts/install_python_deps.sh` need different privileges, so
they run separately here: the Go build stays as the invoking user, while
`uv pip install --system` writes under `/usr/local` and needs root.

Step 4's check is not decoration. Without `node_modules/.bin/next`, `bun run dev`
falls back to `PATH` and finds nmh's `/usr/bin/next`, which exits 1 with a
message about mail being unconfigured.

## 20. Where the logs are

Three places, and choosing the wrong one wastes the most time.

| Location | Contents | When |
| -------- | -------- | ---- |
| `/tmp/task_run.log` | the three services' stdout. **With `LOG_STDOUT=true` the workers' output is here too** | services will not start, `/start` fails, WebSocket proxy errors |
| `$LOG_PATH/app-<channel>-<ts>.log` | one file per worker (`LOG_PATH` defaults to `/tmp/ten_agent`) | conversation behaviour — most problems |
| `$LOG_PATH/property-<channel>-<ts>.json` | the graph configuration that session used | confirming an injected value |

**Do not guess which.** The API server records both in its own `handlerStart`
line:

```bash
grep -o 'LogFile:[^ ]*'     /tmp/task_run.log | tail -1
grep -o 'Log2Stdout:[a-z]*' /tmp/task_run.log | tail -1
```

`Log2Stdout:true` means the per-worker file may not exist at all while the worker
runs normally. This resolves it either way:

```bash
L=$(F=$(grep -o 'LogFile:[^ ]*' /tmp/task_run.log | tail -1 | cut -d: -f2); \
    [ -s "$F" ] && echo "$F" || echo /tmp/task_run.log)
```

Pipeline sections are keyed by **node name**, which does not change when a vendor
is swapped:

```bash
grep -E '\[stt\]|asr_result|asr_error'          "$L" | tail -20
grep -E '\[llm\]|main_control'                  "$L" | tail -20
grep -E '\[tts\]|tts_audio_start|tts_audio_end' "$L" | tail -20
```

The first line to read in each is `config:` — what the extension actually
received. An empty credential looks identical to a working one everywhere else.

`key_point` is a log category extensions use to mark significant events
(`LOG_CATEGORY_KEY_POINT`); grepping it yields the pipeline's skeleton.

Test runs write to neither — see 18.4.

## 21. Diagnosing by symptom

One command to locate the failing stage:

```bash
L=$(F=$(grep -o 'LogFile:[^ ]*' /tmp/task_run.log | tail -1 | cut -d: -f2); \
    [ -s "$F" ] && echo "$F" || echo /tmp/task_run.log)
grep -E 'key_point|_error:|connection_status_changed|Failed to|Traceback|Worker process failed' "$L" | tail -40
```

| Seen | Broken | Go to |
| ---- | ------ | ----- |
| nothing, and no log file | the worker never started | 21.2 |
| `Worker process failed` | the worker exited | 21.2 |
| `proxy error … ECONNREFUSED` | WebSocket | 21.3 |
| ASR `connection_status_changed … 'disconnected'` | ASR | 21.5 |
| `asr_result` but no `[llm]` | routing, or `main_control` | 21.6 |
| `[llm]` but no TTS audio | TTS | 21.7 |
| `Failed to find destination of a … message` | the graph is missing a connection | 3.4 |

### 21.1 Services will not start

**Stopped by SIGTTOU.** A backgrounded `task run` gets stopped when it touches
the terminal. `nohup` blocks SIGHUP, not this.

```bash
ps -o pid,stat,cmd -p $(pgrep -f 'task run|bin/api|tman designer' | tr '\n' ',' | sed 's/,$//')
```

`T` in `STAT` means stopped. **A stopped process keeps its listening socket**, so
a port check reports it as healthy while it accepts nothing. Use tmux:

```bash
tmux new -d -s ten 'cd <example> && task run 2>&1 | tee /tmp/task_run.log'
```

Detach with <kbd>Ctrl+B</kbd> then <kbd>D</kbd> — never <kbd>Ctrl+C</kbd>.

**Port 3000 taken.** Check `ss -tlnp | grep :3000` and `docker ps`. A container
publishing 3000 on the same host — or reachable through WSL2's localhost
forwarding — serves a different application at the address you expect.

**The frontend exits immediately** with a message about nmh: the playground's
`node_modules` is missing. See 19.5.

### 21.2 The worker will not start

```bash
curl -s localhost:8080/list          # "data":[] means none
grep -E 'handlerStart|Worker (start|stop|process)|placeholder' /tmp/task_run.log | tail -12
```

A worker that dies early **produces no log file of its own**, and the only record
is `task_run.log`.

The ten seconds before `Worker process failed` are not a timeout.
`worker_linux.go:83-93` retries `pgrep -P <pid>` ten times at one-second
intervals to find the child; a process that died immediately fails all ten. The
worker exited at once.

Reproduce it in the foreground, where the error is visible:

```bash
cd <example>/tenapp && tman run start
```

`No such file or directory … bin/main` with exit 127 means the Go app was never
built — see 19.5.

**`${env:}` with no default.** `Environment variable X is not found, neither
default value is provided` followed by `exit status 1` is
`placeholder.c:196`, and the process death that follows is line 207. The usual
cause is that `.env` was filled *after* `task run` started; it is read once at
startup.

```bash
stat -c '%y' ~/ten-framework/ai_agents/.env
ps -o lstart= -p $(pgrep -f 'bin/api' | head -1)
```

`.env` newer than the process means restart, and try nothing else first.

### 21.3 WebSocket will not connect

`ws.onerror` carries no detail — the browser's error event never does. Check the
server side.

```
browser  ws://localhost:3000/ws/{port}      port is random 8000-9000, kept in localStorage
   ↓     frontend/server.js proxies
         ws://127.0.0.1:{port}
   ↓     the API server injects it into the graph
         websocket_server binds it
```

The client connects only after `/start` returns, and then after a fixed two-second
delay; failures retry every three seconds indefinitely.

```bash
grep 'WebSocket upgrade'     /tmp/task_run.log | tail -2   # the port the browser wants
grep 'WebSocket proxy error' /tmp/task_run.log | tail -2
grep -o "\[websocket_server\] Loaded config: {[^}]*}" "$L"  # the port the extension bound
```

| Seen | Means |
| ---- | ----- |
| the third command prints nothing | the worker never started — 21.2 |
| the ports differ | injection failed |
| one or two `ECONNREFUSED`, then quiet | **normal** — the browser knocked a few hundred ms early and the retry connected |
| `ECONNREFUSED` forever | the worker died — 21.2 |

### 21.4 No microphone

`Cannot read properties of undefined (reading 'getUserMedia')` at
`audioUtils.ts:178` means `navigator.mediaDevices` is `undefined`. **The whole
API is absent, not blocked**: browsers expose it only in a secure context —
HTTPS, or `localhost`. An IP address never qualifies.

```js
[location.href, window.isSecureContext, !!navigator.mediaDevices]
```

Tunnel from the machine running the browser, then open `http://localhost:3000` —
not the IP. Any port on `localhost` is a secure context, so a busy 3000 is no
obstacle:

```bash
ssh -L 3100:localhost:3000 <user>@<host>
```

### 21.5 No ASR results

```bash
grep -E '\[stt\]|asr_result|asr_error|connection_status_changed' "$L" | tail -25
```

| Line | Means |
| ---- | ----- |
| `[stt] config: {…'params':{…}}` | **read this first** — `'api_key': ''` is an unfilled key |
| `connection_status_changed … 'connected'` | the vendor accepted the connection |
| `send asr_error: {"code": …, "message": …}` | the vendor's own reason |

`_send_asr_metrics … 'actual_send': 0` means **no audio bytes were sent at all** —
look upstream at the microphone (21.4) or the WebSocket (21.3), not at the ASR.

If the interface shows nothing while the log has the error, the graph is not
routing `error` — see 3.4.

### 21.6 No LLM response

```bash
grep -E '\[llm\]|asr_result|main_control' "$L" | tail -25
```

| Seen | Broken |
| ---- | ------ |
| no `asr_result` at all | ASR — 21.5 |
| `asr_result` but never `[llm]` | `main_control` is not forwarding; check `connections` |
| `[llm]` then an HTTP status | the vendor: 401 key, 404 model name, 429 quota |
| `[llm]` then silence | cannot reach the vendor; `OPENAI_PROXY_URL` exists for this |

`_run_stream … stream error (rid=…)` comes from `ten_ai_base`'s LLM base class
wrapping the vendor exception — **the reason follows on the next lines**, so read
past the first.

### 21.7 No TTS audio

```bash
grep -E '\[tts\]|tts_audio_start|tts_audio_end|pcm_frame' "$L" | tail -25
```

`tts_audio_start`, a `tts_ttfb` metric and `tts_audio_end` with a non-zero
`request_total_audio_duration_ms` mean synthesis succeeded and the problem is
downstream. Confirm the audio left the server:

```bash
grep 'Forwarded .* bytes of audio' "$L" | tail -3
```

That line is `log_debug`, so its absence proves nothing unless debug is enabled.
Turn it on in `tenapp/property.json`, restart the session, and turn it back
afterwards — it is very loud:

```bash
sed -i 's/"level": "info"/"level": "debug"/' <example>/tenapp/property.json
```

Audio forwarded but nothing audible is a browser-side problem. `AudioPlayer`
creates its `AudioContext` in a mount effect, before any user gesture, so
browsers return it suspended; `start()` on a buffer source then produces no sound
and raises nothing, and because `onended` never fires the queue stalls on the
first buffer. The capture side already resumes; the player did not until
`aa98a8ffc`.

Leave `output_format` at `pcm_16000`. It is what the pipeline agreed on, and
changing it corrupts audio downstream without any error.

## 22. Quick reference

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| `Environment variable X is not found, neither default value is provided` | absent from `.env`, or `task run` older than `.env` | fill it, then **restart** |
| `config: {…'api_key': ''}` | present but empty | fill it, then restart |
| `STAT` shows `T` | backgrounded, stopped by SIGTTOU | use tmux |
| `Cannot read properties of undefined (reading 'getUserMedia')` | not a secure context | SSH tunnel, `localhost` |
| `proxy error … ECONNREFUSED` continuing | the worker is not running | 21.2 |
| `Failed to find destination of a … message` | the graph lacks a connection | add it to `connections` |
| `actual_send: 0` | no audio is reaching ASR | 21.3, 21.4 |
| exit 127, `bin/main` not found | the Go app was never built | 19.5 |
| `next: Doesn't look like nmh is installed` | playground `node_modules` missing | 19.5 |
| the UI is silent while the log has the error | `error` is unrouted | **trust the log** |
| a vendor swap changed nothing | `addon` edited without a restart | 17.5 |
| the browser shows a different application | port 3000 taken by something else | `ss -tlnp \| grep :3000` |

---

# Part VI — Status

## 23. Worth fixing upstream

- **Publish `agora_rtc` and `agora_rtc_sdk` for arm64.** Both build for aarch64
  today — the wrapper needs no change to its logic — but neither is in the
  registry, so every consumer builds and places them by hand, outside the package
  manager (16.2). Publishing them unblocks 24 of the 26 examples with no work on
  the consumer's side.

- **`#include <cstdint>` in the `agora_rtc` source.** GCC 13 stopped including it
  transitively and the source relies on it in 27 files. This breaks the x64 build
  on a current toolchain too, so it is not an arm64 concern.

- **An aarch64 `liblinux_audio_hy_extension.so`,** or confirmation that it is no
  longer used. It currently ships x86-64 only and has to be excluded from arm64
  packages.

- **The arm64 glibc floor.** Building `linux_arm64.yml` on `ubuntu-22.04-arm`, or
  against an older sysroot, would drop the requirement from 2.38 back to ~2.34
  and make the packages usable on RHEL 9, Amazon Linux 2023 and Ubuntu 22.04.

- **The Python version mismatch.** The arm64 `ten_runtime_python` is built
  against 3.12 while `python_addon_loader` defaults to `libpython3.10.so`. One of
  the two should move so that no `TEN_PYTHON_LIB_PATH` is needed.

- **`tman check env` guidance.** It reports Python 3.10 as the only supported
  version, which contradicts the arm64 artefacts.

- **`uv pip install --system` is hardcoded, so a bare-metal install needs root.**
  `install_python_deps.py` builds the command literally
  (`["uv", "pip", "install", "--system"]`) with no env override. Inside the
  container this is free — it runs as root — but as an ordinary user every
  extension fails with `Permission denied`. Honouring `PIP_INSTALL_CMD` the way
  `install_deps_and_build.sh` already does would be enough.

- **Nothing pins the interpreter for dependency installs.** `--system` resolves
  to whatever `python3` is, which on any distribution shipping 3.13+ is not the
  3.12 the arm64 binding was built against. The mismatch is silent until an
  extension raises `ModuleNotFoundError` at runtime. Selecting the interpreter
  from `TEN_PYTHON_LIB_PATH` — the version is already stated there — would close
  the gap without a new setting.

- **`${env:}` validation misses nested keys.** The API server checks only a
  node's top-level string properties, and every vendor key lives under `params`
  (17.3). A missing or empty key produces no warning from the server.

- **`ten_agent_build` is single-arch.** Publishing it as a multi-arch manifest
  the way `ten_building_ubuntu2204` already is would remove the need for
  `Dockerfile.dev` entirely.

- **`rustup target add stable x86_64-unknown-linux-gnuasan`** is hardcoded in the
  multi-arch builder Dockerfile and has no meaning on arm64.

## 24. Not covered here

- **A from-source core build has not been exercised on arm64 beyond CI.** The
  official job builds it; nothing in this manual verified the result
  independently, and no test target has ever been built there (18.3).

- **The guarder suites have never run on arm64.** The path is
  architecture-correct; that is not the same as having been run.

- **`ffmpeg` and `libwebsockets` are untested in the arm64 core build.**

- **`build_docker_for_ai_agents.yml`** builds the 13 example images without a
  `platforms:` field, so they are amd64-only.

- **Concurrency and long-running behaviour.** Everything verified here is a
  single session on an idle machine.

- **The 25 examples other than `websocket-example` and `voice-assistant`.** Their
  extensions are pure Python and their graphs differ only in configuration, so
  they are expected to work — but expectation is not verification.
