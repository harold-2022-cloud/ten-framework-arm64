# Building and running on arm64

Status of arm64 support across the two halves of the repo, the ABI constraints
that decide which distributions work, and the steps for both a bare-metal and a
containerised setup.

## Summary

| Half | arm64 status |
| ---- | ------------ |
| Framework core (`core/`, `packages/`, `third_party/`) | Supported. `linux_arm64.yml` builds it natively; all third-party deps are source-built. |
| AI agents — non-RTC examples | Supported, on a distro that meets the ABI baseline below. |
| AI agents — RTC examples (19 of 21) | **Blocked**, but narrowly — see "The Agora gap" below. The SDK has an aarch64 build; the TEN extension wrapper does not. |

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
| Fedora 39+ | 2.38+ | 3.12 | Works |
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

```bash
file ai_agents/server/bin/api                    # => ARM aarch64
find ai_agents/agents/examples/websocket-example/tenapp/ten_packages \
  -name '*.so' | head -1 | xargs file            # => ARM aarch64
```

`.github/workflows/ai_agents_arm64.yml` runs this path on `ubuntu-24.04-arm`
and asserts the glibc floor, the libpython redirect, the resolved lock platform
and the ELF architecture.

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
  RTSA SDK, so only the TEN wrapper is missing. This one change unblocks 19 of
  the 21 examples.

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

## Known remaining gaps

- `build_docker_for_ai_agents.yml` builds the 13 example images without a
  `platforms:` field, so they are amd64-only.
- Guarder integration tests have not been run on arm64.
- `ffmpeg` and `libwebsockets` are untested in the arm64 core build.
