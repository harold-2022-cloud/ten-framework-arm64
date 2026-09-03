# Building and running on arm64

Status of arm64 support across the two halves of the repo, the ABI constraints
that decide which distributions work, and the steps for both a bare-metal and a
containerised setup.

## Summary

| Half | arm64 status |
| ---- | ------------ |
| Framework core (`core/`, `packages/`, `third_party/`) | Supported. `linux_arm64.yml` builds it natively; all third-party deps are source-built. |
| AI agents — non-RTC examples | Supported, on a distro that meets the ABI baseline below. |
| AI agents — RTC examples (24 of 26) | **Blocked**, but narrowly — see "The Agora gap" below. The SDK has an aarch64 build; the TEN extension wrapper does not. |

`tman`'s own environment check lists `linux/aarch64` as a first-class supported
platform, alongside `linux/x86_64`, `macos/x86_64`, `macos/aarch64` and
`windows/x86_64` (`core/src/ten_manager/src/check_env/check_os.rs`). Docker is
not required anywhere; it only supplies a userspace that happens to satisfy the
constraints below.

## Running without Docker: the ABI baseline

Two hard constraints govern whether the published arm64 packages will load on a
given machine. Both are properties of the binaries, verified with `readelf`
against `ten_packages-linux-arm64-gcc-release.zip` at 0.11.71.

### 1. glibc >= 2.38

| Library | Max glibc symbol required |
| ------- | ------------------------- |
| `libten_utils.so` | `__isoc23_strtol@GLIBC_2.38` |
| `libpython_addon_loader.so` | `__isoc23_strtol@GLIBC_2.38` |
| `libten_runtime.so` | `GLIBC_2.34` |
| `libten_runtime_python.so` | `GLIBC_2.17` |

The x64 packages top out at `GLIBC_2.34` for the same libraries. The difference
is not a deliberate API choice — `__isoc23_strtol` is what glibc's headers
redirect `strtol` to on glibc 2.38+ with a recent GCC. It is an artifact of
`linux_arm64.yml` running on `ubuntu-24.04-arm` while `linux_ubuntu2204.yml`
runs on `ubuntu-22.04`. It nonetheless raises the arm64 runtime floor by four
glibc releases.

### 2. Python version must match the binding

`ten_runtime_python` is compiled against a specific Python's headers, and
`python_addon_loader` `dlopen`s libpython by hardcoded filename:

| | x64 packages | arm64 packages |
| --- | --- | --- |
| `ten_runtime_python` built against | Python 3.10 headers | **Python 3.12 headers** |
| `python_addon_loader` default dlopen target | `libpython3.10.so` | `libpython3.10.so` |

The arm64 pair is internally inconsistent: the binding wants 3.12, the loader
looks for 3.10. The loader accepts an override, so the fix is to point it at the
Python the binding was actually built against:

```bash
export TEN_PYTHON_LIB_PATH=/usr/lib/aarch64-linux-gnu/libpython3.12.so
```

Without it you get `[Python addon loader] Failed to load system libpython.` on
any distro that has no `libpython3.10`.

Note that `tman check env` still reports *"TEN Framework only supports Python
3.10"* and suggests `pyenv install 3.10.18`. On arm64 that advice is wrong for
the published packages — following it gives you a 3.10 interpreter against a
3.12-built binding.

### Distribution compatibility

| Distribution | glibc | System Python | Prebuilt arm64 packages |
| ------------ | ----- | ------------- | ----------------------- |
| Ubuntu 24.04 LTS | 2.39 | 3.12 | **Works** — set `TEN_PYTHON_LIB_PATH` |
| Ubuntu 24.10 / 25.04 | 2.40+ | 3.12+ | Works if libpython matches 3.12 |
| Debian 13 (trixie) | 2.41 | 3.13 | Needs a 3.12 libpython installed |
| Fedora 39 / 40 | 2.38 / 2.39 | 3.12 | **Works** — `TEN_PYTHON_LIB_PATH` is under `/usr/lib64` |
| Fedora 41+ | 2.40+ | 3.13 | **Works** — needs `python3.12` installed alongside; see the Fedora section |
| Lychee 2025 (Fedora rebuild) | 2.41 | 3.13 | **Verified** — the platform the RPM section was written on |
| Ubuntu 22.04 LTS | 2.35 | 3.10 | **No** — below the glibc floor |
| Debian 12 (bookworm) | 2.36 | 3.11 | **No** |
| RHEL / Rocky 9 | 2.34 | 3.9 | **No** |
| Amazon Linux 2023 | 2.34 | 3.9 | **No** — relevant for Graviton |
| Alpine (musl) | — | — | **No** — no musl builds exist |

Ubuntu 24.04 arm64 is the path of least resistance, and it is what CI uses.

If you are on a distribution below the glibc floor — notably Amazon Linux 2023
or RHEL 9 on Graviton — do not fight the prebuilt packages. Build the core from
source instead (next section); the result links against your own glibc and your
own Python, which sidesteps both constraints.

## The Agora gap

RTC support is split across two TEN packages, and only one of them is missing for
arm64.

| Package | Contents | aarch64 |
| ------- | -------- | ------- |
| `agora_rtc_sdk` (system) | Agora's own SDK: `libagora_rtc_sdk.so`, `libaosl.so`, codec libs, headers | **Available**, outside the TEN store |
| `agora_rtc` (extension) | `libagora_rtc.so` — the wrapper implementing TEN's extension interface on top of the SDK | **Missing, and unbuildable locally** |

Agora publishes an aarch64 Linux RTSA SDK build, e.g.
`Agora-RTC-aarch64-linux-gnu-v4.4.32-20250425_150503-675674.tgz` from
`download.agora.io`. Its libraries are genuinely portable — `libagora_rtc_sdk.so`
needs only `GLIBC_2.18`, `libaosl.so` and `libagora-fdkaac.so` only `GLIBC_2.17`,
which is *lower* than TEN's own arm64 packages require.

The blocker is the other package. The published `agora_rtc` extension contains
exactly four files — `manifest.json`, `property.json`,
`lib/libagora_rtc.so` and `lib/liblinux_audio_hy_extension.so` — with no source.
There is no Agora SDK consumer anywhere in this repo (`IAgoraRtcEngine`,
`NGIAgoraRtcConnection`, `createAgoraService` return nothing outside
`third_party`), no `agora_rtc` source directory, and nothing in git history. Only
Agora or the TEN maintainers can produce the arm64 build.

### The aarch64 SDK is the right SDK

At first glance the aarch64 tarball looks like the wrong product: 45 of the x64
package's 109 headers are absent from it, including `IAgoraRtcEngine.h`,
`IAgoraRtcEngineEx.h`, `IAgoraMediaEngine.h` and `IAgoraMusicContentCenter.h`.
The x64 package is assembled from the full RTC SDK; the aarch64 tarball is the
RTSA / server SDK.

Symbol analysis says that does not matter. The x64 `libagora_rtc.so` resolves
exactly **two** symbols from `libagora_rtc_sdk.so`:

```
createAgoraService      # the low-level / server SDK entry point (IAgoraService.h)
getAgoraSdkVersion
```

It references **no** `agora::` class symbols directly — everything else goes
through vtables — and it never touches `createAgoraRtcEngine` or `IRtcEngine`.
So the wrapper is built against the low-level API exclusively, which is precisely
what the aarch64 RTSA SDK provides. Both symbols are exported by the aarch64
`libagora_rtc_sdk.so`.

### Two real gaps to name in the request

- **Two libraries the wrapper hard-links are absent from the aarch64 bundle.**
  `libagora_rtc.so` lists `libagora-ffmpeg.so` and `libagora-soundtouch.so` as
  `NEEDED`; the aarch64 tgz ships only `libagora_rtc_sdk.so`, `libaosl.so` and
  `libagora-fdkaac.so`. The x64 TEN package ships eight. Either the aarch64
  bundle needs them, or the aarch64 wrapper must be linked without them.
- **Build number differs.** The lock pins `4.4.32-141`; the aarch64 tgz reports
  `4.4.32` with no build suffix. Same minor version, different build.

So the ask is specific and small: an aarch64 build of the `agora_rtc` TEN
extension, plus an aarch64 SDK bundle carrying the same library set as the x64
one. Until then, `websocket-example` and the `websocket_server` extension are the
only transport path on arm64.

### Packaging the aarch64 SDK ahead of time

The SDK half can be prepared now, so that the day an aarch64 wrapper appears
there is nothing else to build.
`ai_agents/agents/scripts/package_agora_rtc_sdk_arm64.sh` converts Agora's
tarball into a TEN `agora_rtc_sdk` system package: it refuses to run unless the
libraries really are aarch64, maps the flat `include/` tree onto the
`include/rtc/low_level_api/include/` layout the x64 package uses, stamps
`supports: linux/arm64`, and writes a `PROVENANCE.md` into the package recording
where it came from and how it differs from the published x64 one.

```bash
TMAN=/path/to/tman ai_agents/agents/scripts/package_agora_rtc_sdk_arm64.sh \
  Agora-RTC-aarch64-linux-gnu-v4.4.32-20250425_150503-675674.tgz \
  out/arm64
# => out/arm64/agora_rtc_sdk-4.4.32-141-linux-arm64.tpkg
```

It stamps `4.4.32-141` by default — not the upstream build number — because the
`agora_rtc` extension pins `"version": "=4.4.32-141"` exactly and nothing else
will resolve as its dependency. Override with `PKG_VERSION=` if a future wrapper
pins something different. The mismatch is recorded in the package's
`PROVENANCE.md` rather than left implicit.

To consume it before it exists in any public registry, publish it to a local
`file://` registry — `tman` supports that scheme directly:

```bash
mkdir -p /srv/ten-localreg
cat > /tmp/tman-local.json <<'JSON'
{ "registry": { "default": { "index": "file:///srv/ten-localreg" } } }
JSON

# publish (run from an extracted copy of the package)
mkdir pkg && tar xzf out/arm64/agora_rtc_sdk-*.tpkg -C pkg
(cd pkg && tman -c /tmp/tman-local.json publish)

# install into a tenapp, resolving for arm64 explicitly
tman -c /tmp/tman-local.json -y install --os linux --arch arm64
```

Verified end to end: the package conforms to the manifest JSON schema, resolves
against the `=4.4.32-141` exact pin, installs into a tenapp, and produces a lock
entry reading `{"os": "linux", "arch": "arm64"}`. Note that `--os` and `--arch`
let you resolve for a target platform from a different host, which is useful for
staging an arm64 tenapp on an x86 machine.

## Framework core from source

`core/ten_gn` is a submodule and supplies `tgn` itself, so initialize it first.

```bash
git submodule update --init --recursive --depth 1 core/ten_gn
export PATH=$(pwd)/core/ten_gn:$PATH
```

Toolchain required: clang or gcc, cmake, Go >= 1.20, Rust stable with
`cbindgen`, Python 3 with `python-dotenv` and `jinja2`, Node (only if building
the tman designer frontend). `tools/docker_for_building/ubuntu/22.04/Dockerfile`
is the authoritative dependency list — read it as a package manifest even if you
never build the image.

The root `Taskfile.yml` detects the host arch, so on arm64 this targets arm64
with no extra flags:

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

Two differences from the x64 build: arm64 CI does not enable
`ten_enable_ffmpeg_extensions`, and it turns `ten_enable_libwebsockets` off.
Re-enable either only with time budgeted to debug it.

Cross-compiling is not set up. CI only ever builds arm64 on native arm64
runners; there is no sysroot or cross-toolchain configuration in this repo.

## Component inventory, and what arm64 changes

The build graph is easier to reason about than the package list it produces.
`BUILD.gn` defines one group, `ten_framework_all`, and everything else hangs off
it:

| Target | Contents |
| ------ | -------- |
| `core/src/ten_runtime` | the C runtime |
| `core/src/ten_runtime/binding` | the go, nodejs and python bindings |
| `core/src/ten_rust` | Rust crates, gated on `ten_enable_ten_rust` |
| `core/src/ten_manager` | `tman`, gated on `ten_enable_ten_manager` |
| `third_party` | fourteen dependencies, all source |
| `packages/core_addon_loaders` | `python_addon_loader`, `nodejs_addon_loader` |
| `packages/core_apps` | `default_app_{cpp,go,nodejs,python}` |
| `packages/core_extensions` | nine `default_*` templates (extension, async, asr, tts, llm, mllm) |
| `packages/core_protocols` | `msgpack` |
| `packages/core_systems` | `pytest_ten` |
| `packages/example_apps` | `pprof_app_go`, `transcriber_demo` |
| `packages/example_extensions` | eighteen, including the ffmpeg trio, `vosk_asr_cpp`, `webrtc_vad_cpp` |

Twenty-two feature flags govern that graph, seven in `build/options.gni` and
fifteen in `build/ten_runtime/options.gni`.

### What is actually unavailable or disabled on arm64

Only the first of these is a functional gap. The rest are worth knowing so they
are not mistaken for one.

| Item | Nature |
| ---- | ------ |
| The `agora_rtc` extension | The only real gap. No aarch64 artifact in the registry, no source in the tree, so it cannot be built locally either. |
| Coverage instrumentation | Cannot be enabled at all. Six `assert(is_linux && target_cpu == "x64")` guard it — three in `build/ten_runtime/glob.gni`, two in `build/ten_runtime/ten.gni`, one in `build/ten_common/rust/rust.gni`. All are inside `if (enable_coverage)`, so the default build never reaches them. |
| Tests | `linux_arm64.yml` sets `ten_enable_tests=false` along with the rust and manager test flags, so the arm64 CI job builds no test target at all. |
| `ten_enable_libwebsockets=false` | Costs nothing. The flag is referenced in exactly three places, all under `tests/ten_runtime/`, and arm64 CI already disables tests. It gates no runtime code. |
| `ten_manager_enable_frontend=false` | The tman designer's frontend is not built. |
| `ten_enable_go_app_leak_check` | Defined as x64-only in `build/ten_runtime/options.gni`, and only meaningful under a sanitizer debug build. |
| `rustup target add stable x86_64-unknown-linux-gnuasan` | Hardcoded in the build Dockerfile and meaningless on arm64. |
| ffmpeg extensions | Off by default everywhere (`ten_enable_ffmpeg_extensions = false`), not an arm64 restriction. |

The fourth row corrects a plausible misreading: turning `libwebsockets` off
looks like dropping a transport, and is not.

## Build dependencies, extracted from the Dockerfile

`tools/docker_for_building/ubuntu/22.04/Dockerfile` is the only place the core
build's dependencies are written down, and it is Ubuntu 22.04 and Debian
tooling throughout. On any other distribution it has to be read as a manifest
rather than run. Grouped by purpose:

| Purpose | Packages (Ubuntu names) |
| ------- | ----------------------- |
| Compiler and build | `build-essential` `cmake` `make` `autoconf` `libtool` `pkg-config` |
| Crypto and network | `libssl-dev` `libcurl4-gnutls-dev` `libcrypto++-dev` `libnss3-dev` |
| Parsing and serialisation | `libexpat1-dev` `libmsgpack-dev` `zlib1g-dev` |
| System | `uuid-dev` `libunwind-dev` `libffi-dev` `libreadline-dev` `libncurses5-dev` `libgdbm-dev` |
| Audio | `libasound2` |
| ffmpeg extensions only | `libavformat-dev` `libavfilter-dev` `libx264-dev` `libdrm-dev` `libxcomposite-dev` `libxdamage1` |
| Sanitizer | `libasan5` |
| Python | `python3` `python3-dev` `python3-pip` `python3-venv` |
| Toolchains | Go 1.22.3 plus go1.20.12 for compatibility checks; Rust stable with `cbindgen`; clang-18 from apt.llvm.org |
| Tools | `uv` `task` `jq` `git` `zip` `unzip` `p7zip-full` `tree` `cpulimit` `iwyu` |

Two entries have no consumer anywhere in the GN graph — `libmysqlclient-dev`
and `libmysqlcppconn-dev` — and the ffmpeg row is only needed with
`ten_enable_ffmpeg_extensions=true`, which nothing sets.

Three lines in that Dockerfile do not survive a move off Ubuntu x64:

```dockerfile
ln -sf /usr/bin/python3.10-config /usr/bin/python3-config   # hardcodes 3.10
rustup target add stable x86_64-unknown-linux-gnuasan       # hardcodes x86_64
add-apt-repository "deb http://apt.llvm.org/..."            # Debian-only, for clang-18
```

The Go install is the counter-example worth copying: it derives the archive name
from `dpkg --print-architecture`, so it is already arch-correct — though `dpkg`
itself has to be replaced on an RPM distribution.

## AI agents without Docker

On Ubuntu 24.04 arm64. Adjust package names for other distributions.

```bash
# 1. System packages
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential cmake pkg-config git curl unzip jq \
  python3 python3-dev python3-pip python3-venv \
  libasound2t64 libunwind-dev libssl-dev libc++1 libgstreamer1.0-dev

# 2. The Python ABI redirect -- see the ABI baseline above
export TEN_PYTHON_LIB_PATH=/usr/lib/aarch64-linux-gnu/libpython3.12.so
test -e "$TEN_PYTHON_LIB_PATH"

# 3. Go, Node, Bun
curl -fsSLO https://go.dev/dl/go1.24.3.linux-arm64.tar.gz
sudo tar -C /usr/local -xzf go1.24.3.linux-arm64.tar.gz
export PATH=/usr/local/go/bin:$PATH
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs
curl -fsSL https://bun.sh/install | bash && export PATH="$HOME/.bun/bin:$PATH"

# 4. uv, task, tman -- all have arm64 builds
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
sudo sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b /usr/local/bin
curl -fsSL -o /tmp/tman.zip \
  https://github.com/TEN-framework/ten-framework/releases/download/0.11.71/tman-linux-release-arm64.zip
unzip -q /tmp/tman.zip -d /tmp/tman
sudo install -m 0755 /tmp/tman/ten_manager/bin/tman /usr/local/bin/tman

# 5. Confirm the environment before building anything
tman check env
```

Then build an example. Only ones without an Agora dependency will resolve;
`websocket-example` is the reference:

```bash
cd ai_agents
cp .env.example .env      # fill in provider keys
cd agents/examples/websocket-example
task install
task run
```

`task install` rewrites `tenapp/manifest-lock.json` with arm64 entries. That is
expected — see "Lockfiles" below.

### Python dependencies

Every extension in `ai_agents/agents/ten_packages/extension/` is pure Python
with no bundled binaries, so nothing there needs compiling for arm64. The
aggregate dependency set across all extensions includes `numpy`, `scipy`,
`torch`, `pillow`, `cryptography` and `soundfile`, all of which publish
`manylinux` aarch64 wheels.

`install_python_deps` installs requirements for every extension in the tenapp,
not just the ones a graph uses. For `websocket-example` that resolves to
`numpy` as the only native-wheel dependency — no `torch`, no `scipy`.

### Lockfiles

A committed `manifest-lock.json` records only the platform resolved on the
machine that generated it, not the package's platform matrix. Every example lock
in this repo reads `{"os": "linux", "arch": "x64"}`, which looks like a hard
arm64 block and is not one — the same versions are published for arm64.

Nothing in this repo passes `tman install --locked`, so the lock is re-resolved
and rewritten against the host on every install. Read the registry, not the lock.

## AI agents on RPM-based distributions (bare metal)

Verified end to end on an aarch64 Fedora rebuild — Lychee 2025, glibc 2.41,
gcc 14.3.1, dnf5 — as an ordinary non-root user, with no container anywhere.
The preceding section is written for Ubuntu 24.04; what follows is only where
the RPM side diverges from it. None of the differences are in the framework:
every one is environmental.

Everything below applies to stock Fedora, which shares the layout the
differences are about: `/usr/lib64` rather than a multiarch directory, dnf
package names, and parallel-installable Python versions. One item is specific to
a rebuild rather than to Fedora and is marked as such.

### What carried over unchanged

The parts that looked riskiest turned out to be portable. `tman install`
resolved the arm64 packages straight from the registry with no special handling.
The Go binding linked against the arm64 `libten_runtime.so` and `libten_utils.so`
and built clean (`GOARCH=arm64`, `GOARM64=v8.0`, toolchain
`/usr/local/go/pkg/tool/linux_arm64`). And every Python dependency resolved to an
aarch64 wheel — `pydantic_core`, `aiohttp`, `awscrt` and the rest arrived as
`manylinux_*_aarch64`, with nothing falling back to a source build.

### 1. libpython lives in /usr/lib64

Fedora does not use Debian's multiarch layout, so the path given in the ABI
baseline section does not exist here:

```bash
export TEN_PYTHON_LIB_PATH=/usr/lib64/libpython3.12.so
```

The symlink comes from `python3.12-devel`; `python3.12-libs` supplies the
`libpython3.12.so.1.0` it points at.

### 2. Fedora 41+ ships Python 3.13, not 3.12

Ubuntu 24.04 happens to ship exactly the 3.12 the arm64 binding was compiled
against, which is why the Ubuntu path needs no version work at all. Fedora 41
and later default to 3.13, so 3.12 has to be installed alongside it:

```bash
sudo dnf -y install python3.12 python3.12-devel
```

Fedora keeps parallel Python versions installable, so this leaves the system
interpreter alone.

### 3. UV_PYTHON must be pinned

This is the failure with the least helpful symptom, and it does not exist on
Ubuntu. Both dependency installers hardcode `uv pip install --system` —
`install_python_deps.py` in the tenapp, and `PIP_INSTALL_CMD` in
`install_deps_and_build.sh` — and `--system` selects the *default* interpreter,
which on Fedora 42 is 3.13. The dependencies land in 3.13's site-packages while
the runtime loads 3.12 through `TEN_PYTHON_LIB_PATH`. Nothing fails at install
time; it surfaces much later as `ModuleNotFoundError` inside an extension.

```bash
export UV_PYTHON=/usr/bin/python3.12
```

### 4. `uv pip install --system` needs root

`--system` writes to `/usr/local/lib/python3.12/site-packages` and its `lib64`
sibling. Inside the container this is invisible, because the container runs as
root. On bare metal as an ordinary user, `task install` fails three retries deep
on every extension with `Permission denied (os error 13)`.

Elevate only that one step. Do not `sudo task install` — that would run
`bun install` and `go build` as root too, leaving root-owned artifacts in the
user's home:

```bash
cd ai_agents/agents/examples/<example>/tenapp
sudo env "PATH=$PATH" UV_PYTHON=/usr/bin/python3.12 python3 scripts/install_python_deps.py
```

`task install` aborts at that step, so the two after it have to be finished by
hand as the normal user:

```bash
(cd ai_agents/agents/examples/<example>/frontend && bun install)
(cd ai_agents/server && go mod tidy && go mod download && go build -o bin/api main.go)
```

Both `/usr/local/lib/python3.12/site-packages` and `/usr/local/lib64/...` are on
`/usr/bin/python3.12`'s default `sys.path` on Fedora, so the embedded
interpreter finds what lands there.

### 5. Package names

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

### 6. Installers keyed on os-release (rebuilds only)

This one does not apply to stock Fedora. `https://rpm.nodesource.com/setup_20.x`
exits with `Error: This script is intended for RPM-based systems.` on a rebuild
whose `/etc/os-release` carries its own `ID` and leaves `ID_LIKE` unset — the
script enumerates distributions by that field and never looks at whether `rpm`
is present. Lychee 2025 reports `ID=lychee` with no `ID_LIKE`, so it is refused
despite being an RPM distribution with `dnf5`.

Anything else that reads `ID`/`ID_LIKE` to pick a repository will behave the
same way. The distribution's own package avoids the question entirely, and the
playground runs under `bun` rather than Node, so the Node major version is not
load-bearing:

```bash
sudo dnf -y install nodejs npm
```

### Reading `tman check env` on Fedora

Two of its findings are expected here, and neither blocks an agents build:

| Report | Reality |
| ------ | ------- |
| `⚠️ python3 3.13.9 … only supports Python 3.10` | It inspects `python3`, i.e. the system 3.13. The interpreter that actually runs extensions is the 3.12 behind `TEN_PYTHON_LIB_PATH`. Taking its `pyenv install 3.10.18` advice breaks the build — see the ABI baseline section. |
| `❌ tgn Not installed` | `tgn` is only needed to build the framework core or a C++ extension. `websocket-example`'s `task install` runs `tman install`, `install_python_deps.py`, `bun install` and `go build`, and none of them invoke `tgn`. |

### Firewall

Fedora runs firewalld by default. Browsing from the machine itself is
unaffected; reaching the services from elsewhere needs the ports opened:

```bash
sudo firewall-cmd --add-port=3000/tcp --add-port=8080/tcp --add-port=49483/tcp
```

### SELinux

Relevant only to the container path. Under enforcing, the five bind mounts in
`docker-compose.yml` carry no `:z` label and the container cannot read `/app`;
add the labels through a local `docker-compose.override.yml` rather than editing
the tracked compose file. A host in permissive mode is unaffected.

### Verification result

Verified with `ai_agents/agents/scripts/verify_arm64_install.sh --probe-worker`
on the platform above: **31 checks passed, 0 failed, 2 skipped.**

Read that as a floor, not a verdict. It says one example installs and starts
correctly for this architecture — a single graph, a single throwaway session,
no conversation. The scope block the script prints lists what it leaves alone.

Within that scope, the two findings that matter are the last two, and both are
dynamic — no static check can reach them:

```
8b. Worker probe
  POST /start accepted
  worker for verify-<pid> is registered

9. Worker logs
  no libpython load failure in worker logs
  no ModuleNotFoundError in worker logs
```

A clean worker log settles the architecture question specifically. It means
`python_addon_loader` resolved `libpython3.12` through `TEN_PYTHON_LIB_PATH`
instead of its built-in `libpython3.10.so`, and that the dependencies `uv`
installed are visible to the interpreter the runtime actually embedded — the two
failures the RPM path is prone to, neither of which surfaces until a worker
spawns. It says nothing about whether any particular graph does useful work.

Everything below that is already established by the static sections: every
shared object is aarch64, the Go binding links `libten_runtime.so` and compiles,
and `/graphs` returning the graph definitions proves `property.json` was parsed
by the runtime rather than merely being valid JSON.

Note what this does not cover. A conversation still needs vendor credentials,
which is configuration rather than portability. The framework core was never
built from source here — these are the published packages. And the RTC examples
remain blocked on the missing aarch64 `agora_rtc` build.

## AI agents in a container

The default image `ghcr.io/ten-framework/ten_agent_build:0.7.14` is a
single-arch `linux/amd64` manifest, so `docker-compose.yml` pins
`platform: linux/amd64` and runs under emulation on arm64.

`ai_agents/Dockerfile.dev` builds an arm64 replacement on `ubuntu:24.04`:

```bash
cd ai_agents
docker build -f Dockerfile.dev -t ten_agent_dev:local .
```

Then set both variables in `ai_agents/.env` (see `.env.example`):

```
TEN_AGENT_DEV_IMAGE=ten_agent_dev:local
TEN_AGENT_DEV_PLATFORM=linux/arm64
```

It is deliberately arm64-only and fails the build on amd64. A single image
cannot serve both: x64 packages need Python 3.10 (Ubuntu 22.04) while arm64
packages need glibc 2.38 (Ubuntu 24.04, Python 3.12), and those are mutually
exclusive on one base. amd64 users already have a working image.

Do not run the amd64 image on arm64. It trips the AVX2 probe in
`agents/scripts/install_deps_and_build.sh`, which exists to catch exactly that
case, and aborts with `FATAL: unsupported platform.`

### Verifying you really got arm64

Spot checks:

```bash
file ai_agents/server/bin/api                    # => ARM aarch64
find ai_agents/agents/examples/websocket-example/tenapp/ten_packages \
  -name '*.so' | head -1 | xargs file            # => ARM aarch64
```

For the whole picture, `ai_agents/agents/scripts/verify_arm64_install.sh` covers
host provenance, the module and extension inventory with versions, the ELF
architecture of every shared object rather than a sample, unresolved dynamic
dependencies, the glibc version `libten_runtime.so` actually requires, the
Python ABI wiring, the locally built Go artifacts, and the running services. It
exits with the number of failed checks.

```bash
./ai_agents/agents/scripts/verify_arm64_install.sh                  # static only
./ai_agents/agents/scripts/verify_arm64_install.sh --probe-worker   # also dynamic
```

`--probe-worker` starts a throwaway session through the API and stops it again.
It needs no credentials: a worker that fails to authenticate has still loaded
libpython and instantiated its extensions, which is what the log section reads.
Without it, the check that matters most is reported as skipped rather than
passed — deliberately, since an unexercised Python binding is not evidence of a
working one.

`.github/workflows/ai_agents_arm64.yml` runs this path on `ubuntu-24.04-arm`
and asserts the glibc floor, the libpython redirect, the resolved lock platform
and the ELF architecture.

## Changing a vendor on the one working example

This belongs here because arm64 removes the usual alternative. With 24 of the 26
examples blocked on the missing `agora_rtc` build, `websocket-example` is the
only place to work, so trying a different ASR or TTS provider means editing that
example's graph rather than picking a different example.

### Where the settings live

A vendor's settings go in the node's `property.params`, flat. There is no
wrapper naming the module or the vendor — nothing in this repo reads a
`{"asr": {"vendor": ...}}` shape. Each extension's own test configs are the
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

Swapping a provider means changing `addon` and replacing `params` on that node
in `tenapp/property.json`. Leave `name` and `extension_group` alone — the
`connections` block routes by node name, so renaming the node silently detaches
it from the pipeline.

### `${env:VAR}` has two failure modes, and they look nothing alike

The runtime resolves these placeholders in
`core/src/ten_utils/lib/sys/general/placeholder.c`, recursively through objects
and arrays (`ten_extension_property_resolve_placeholders`,
`core/src/ten_runtime/extension/internal/metadata.c`), so depth does not matter.
What matters is whether the variable exists:

| State | `getenv` | Result |
| ----- | -------- | ------ |
| Not set, no `\|` default | `NULL` | `exit(EXIT_FAILURE)` — the worker dies during property resolution |
| Set but empty | `""` | Resolves to an empty string; the extension rejects it later in its own words |
| Set, with a value | value | Normal |

The `|` in a placeholder supplies a default and makes the variable optional.
`"${env:OPENAI_PROXY_URL|}"` tolerates absence; `"${env:DEEPGRAM_API_KEY}"` does
not. That asymmetry is deliberate: a required key that is simply missing should
stop the process rather than let it run misconfigured.

The empty case is the one that wastes time. Copying `.env.example` gives every
key an empty value, so a key left unfilled is *present* rather than absent — it
takes the middle row, not the first. The worker starts normally and the only
symptom is a vendor error deep in the session log.

### Nothing warns you about a nested key

`ai_agents/server/internal/http_server.go` validates `${env:...}` before
spawning a worker, but the loop reads only a node's top-level properties and
skips any value that is not a string. Every vendor key in these examples lives
under `params`, which is an object, so it is never examined. A missing or empty
vendor key produces no `Environment variable not found` line anywhere.

`verify_arm64_install.sh` covers that gap: it walks the graph for placeholders,
resolves each against `ai_agents/.env`, and separates absent-and-required from
present-but-empty. Run it before starting a session rather than diagnosing from
logs afterwards.

### What a change requires

| Changed | Takes effect |
| ------- | ------------ |
| A value inside `params` (voice id, model, language) | Next session — the server re-reads `property.json` per worker |
| `addon`, node set, or `connections` | Full restart of `task run` |
| `.env` | Full restart — it is read once at startup |

## Testing an ASR, LLM or TTS extension

There are two test harnesses with the same shape and opposite requirements. The
distinction that matters is whether the vendor client is real, because it
decides whether credentials and network are needed.

| | Standalone | Guarder |
| --- | ---------- | ------- |
| Command | `task test-extension EXTENSION=agents/ten_packages/extension/<ext>` | `task asr-guarder-test EXTENSION=<ext>` / `tts-guarder-test` |
| Lives in | `<ext>/tests/` | `agents/integration_tests/{asr,tts}_guarder/` |
| Vendor client | **Mocked** — the client class is patched out | **Real** |
| Credentials | None | The vendor key, from `.env` |
| Network | None | Yes |
| Answers | Does the extension behave correctly given vendor responses | Does the vendor integration actually work |

Both run a real TEN app rather than a stub: each `conftest.py` starts a `FakeApp`
subclass of `App` on its own thread and blocks the fixture until `on_init` fires,
so the C runtime, the Python binding and the addon manager are all live for the
duration. A standalone test is a unit test of the extension, not of a mock.

### How a standalone run works

`<ext>/tests/bin/start` is the entry point:

```bash
export PYTHONPATH=.ten/app:.ten/app/ten_packages/system/ten_runtime_python/lib:...
export TEN_APP_BASE_DIR=.ten/app
pytest -s tests/ "$@"
```

`.ten/app` is a throwaway app tree produced by `tman -y install --standalone`,
which `task test-extension` runs first and deletes afterwards.
`test-extension-no-install` skips both, which is the faster loop while iterating
— and the reason a stale `.ten/` causes confusing failures later.

The mock is per-extension and patches the vendor client where the extension
imports it. For `soniox_asr_python` that is
`soniox_asr_python.extension.SonioxWebsocketClient`, replaced by a `MagicMock`
whose `connect`, `send_audio`, `finalize` and `stop` are `AsyncMock`s, plus
`trigger_open`, `trigger_transcript`, `trigger_error`, `trigger_close` and
`trigger_finished` helpers the test calls to drive the extension through states
the real vendor would produce. Sixteen test files exercise finalize modes,
reconnection, confidence, multilingual output, sentence termination, invalid
params and vendor errors — none of which need a key.

Anything after `--` goes to pytest, which is how a single test is run:

```bash
task test-extension-no-install EXTENSION=agents/ten_packages/extension/soniox_asr_python -- -k test_finalize -s -v
```

### How a guarder run works

The guarder is one harness parameterised by extension name. It rewrites its own
manifest first:

```bash
sed "s/{{extension_name}}/$EXT_NAME/g" manifest-tmpl.json > manifest.json
./scripts/install_deps_and_build.sh <os> <arch>
./tests/bin/start --extension_name <ext> --config_dir <ext>/tests/configs
```

`install_deps_and_build.sh` detects the host architecture when called without
arguments, and the Taskfile passes it explicitly, so this path is arch-correct
on arm64 — though CI has never exercised it there.

`--config_dir` points at the extension's own `tests/configs/`, which is where
the authoritative property shape for that vendor lives: `property_en.json`,
`property_zh.json`, `property_invalid.json`. Those files carry real
`${env:...}` placeholders, so the corresponding key must be set.

Audio fixtures are 16 kHz PCM under `tests/test_data/`: `16k_en_us.pcm`,
`16k_en_us_helloworld.pcm`, `16k_zh_cn.pcm`, `16k_es_es.pcm`.

`tests/bin/start` excludes `test_long_duration_stream` by default. Pass `-k` to
override. Do not run the ASR and TTS guarders concurrently in one container —
their build scripts collide on shared temp paths.

### Where the logs are

Both harnesses configure logging in `conftest.py`, and both send everything to
**stdout at debug level** with no file emitter:

```json
{"ten": {"log": {"handlers": [{"matchers": [{"level": "debug"}],
  "emitter": {"type": "console", "config": {"stream": "stdout"}}}]}}}
```

So there is no log file to find; capture it if you want one:

```bash
task test-extension EXTENSION=agents/ten_packages/extension/soniox_asr_python 2>&1 | tee /tmp/ext_test.log
```

This is a different destination from a running agent, where the API server gives
each worker its own file under `LOG_PATH` (`/tmp/ten_agent` by default). Test
runs never write there.

## Arch selection in the Taskfiles

`ai_agents/Taskfile.yml` detects the host arch and passes it to
`install_deps_and_build.sh`, which also detects it when called with no
arguments. Override either explicitly:

```bash
task build-agent-deps ARCH=arm64
task tts-guarder-test EXTENSION=deepgram_tts ARCH=arm64
./agents/scripts/install_deps_and_build.sh linux arm64
```

## Worth fixing upstream

- **An aarch64 `agora_rtc` extension build.** Agora already ships an aarch64
  RTSA SDK, so only the TEN wrapper is missing. This one change unblocks 24 of
  the 26 examples.

- **arm64 glibc floor.** Building `linux_arm64.yml` on `ubuntu-22.04-arm`, or
  against an older sysroot, would drop the requirement from 2.38 back to ~2.34
  and make the packages usable on RHEL 9, Amazon Linux 2023 and Ubuntu 22.04.
- **Python version mismatch.** The arm64 `ten_runtime_python` is built against
  3.12 while `python_addon_loader` defaults to `libpython3.10.so`. One of the
  two should move so that no `TEN_PYTHON_LIB_PATH` is needed.
- **`tman check env` guidance.** It reports Python 3.10 as the only supported
  version, which contradicts the arm64 artifacts.
- **`ten_agent_build` is single-arch.** Publishing it as a multi-arch manifest
  the way `ten_building_ubuntu2204` already is would remove the need for
  `Dockerfile.dev` entirely.
- **`rustup target add stable x86_64-unknown-linux-gnuasan`** is hardcoded in
  the multi-arch builder Dockerfile and has no meaning on arm64.
- **`uv pip install --system` is hardcoded, so a bare-metal install needs root.**
  `install_python_deps.py` builds the command literally
  (`["uv", "pip", "install", "--system"]`) with no env override, and `--system`
  targets `/usr/local/lib*/python3.x/site-packages`. Inside the container this
  is free — it runs as root — but as an ordinary user every extension fails
  with `Permission denied`. Honouring `PIP_INSTALL_CMD` the way
  `install_deps_and_build.sh` already does would be enough.
- **Nothing pins the interpreter for dependency installs.** `--system` resolves
  to whatever `python3` is, which on any distribution shipping 3.13+ is not the
  3.12 the arm64 binding was built against. The mismatch is silent until an
  extension raises `ModuleNotFoundError` at runtime. Selecting the interpreter
  from `TEN_PYTHON_LIB_PATH` — the version is already stated there — would close
  the gap without a new setting.

## Known remaining gaps

- `build_docker_for_ai_agents.yml` builds the 13 example images without a
  `platforms:` field, so they are amd64-only.
- Guarder integration tests have not been run on arm64.
- `ffmpeg` and `libwebsockets` are untested in the arm64 core build.
