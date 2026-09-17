#!/usr/bin/env bash
#
# Finish an example's install after install_agora_rtc_arm64.sh.
#
# That script places ten_packages/ but stops there. `task install` does four
# more things, and tman run start fails at once without them:
#
#   build the Go app          -> tenapp/bin/main, which scripts/start.sh execs
#   install Python deps       -> every extension's requirements.txt
#   install the frontend      -> the shared playground, not a per-example one
#   build the API server      -> server/bin/api, which task run-api-server execs
#
# The API server is the one that is easy to miss: nothing fails while
# installing, and the omission shows up only as the playground reporting
# ECONNREFUSED against a port nothing is listening on.
#
# The two halves of scripts/install_python_deps.sh need different privileges,
# so they are run separately here rather than through that script: the Go build
# must stay as the invoking user, while `uv pip install --system` writes under
# /usr/local and needs root.
#
# Usage:
#   finish_install_arm64.sh [example]        default: voice-assistant
#
set -euo pipefail

EXAMPLE="${1:-voice-assistant}"
# From the script's own location, not $HOME. A checkout is not always at
# $HOME/ten-framework, and hardcoding it made this build the Go app and
# install the Python packages into a different checkout than the one the
# caller was in -- silently, because both existed and both looked installed.
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
AI_AGENTS="$REPO/ai_agents"
TENAPP="$AI_AGENTS/agents/examples/$EXAMPLE/tenapp"
PLAYGROUND="$AI_AGENTS/playground"

die() { echo "FATAL: $*" >&2; exit 1; }
ok()  { echo "    OK  $*"; }

[[ -d "$TENAPP" ]] || die "no tenapp at $TENAPP"
[[ -f "$TENAPP/manifest.json" ]] || die "no manifest.json in $TENAPP"

# ---------------------------------------------------------------- 1
echo "==> [1/6] Checking prerequisites"
command -v go  >/dev/null || die "go not on PATH"
command -v uv  >/dev/null || die "uv not on PATH"
command -v bun >/dev/null || die "bun not on PATH"

PY_BIN="${UV_PYTHON:-}"
if [[ -z "$PY_BIN" ]]; then
  # The arm64 ten_runtime_python is built against 3.12. Installing into any
  # other interpreter leaves the runtime unable to import what it needs, and
  # nothing reports it until an extension raises ModuleNotFoundError.
  PY_BIN="$(command -v python3.12 || true)"
  [[ -n "$PY_BIN" ]] || die "python3.12 not found and UV_PYTHON unset"
fi
ok "go, uv, bun; python for deps: $PY_BIN"

BUILD_TOOL="$TENAPP/ten_packages/system/ten_runtime_go/tools/build/main.go"
[[ -f "$BUILD_TOOL" ]] || die "$BUILD_TOOL missing -- run tman install first"
ok "go build tool present"

# ---------------------------------------------------------------- 2
echo "==> [2/6] Building the Go app"
# Same command as build_go_app() in scripts/install_python_deps.sh. Run as the
# invoking user so bin/main and the Go cache are not left owned by root.
cd "$TENAPP"
go run "$BUILD_TOOL" --verbose

[[ -f "$TENAPP/bin/main" ]] || die "bin/main was not produced"
file -b "$TENAPP/bin/main" | grep -q 'ARM aarch64' || die "bin/main is not aarch64"
ok "bin/main ($(file -b "$TENAPP/bin/main" | grep -oE 'ARM aarch64'))"

# ---------------------------------------------------------------- 3
echo "==> [3/6] Installing Python dependencies"
# `uv pip install --system` targets /usr/local/lib*/python3.12/site-packages,
# which needs root. Only this step is elevated. UV_PYTHON is passed explicitly
# because sudo does not carry it.
# requirements.txt sits at ten_packages/<kind>/<pkg>/requirements.txt -- depth 3.
REQS=$(find "$TENAPP/ten_packages" -maxdepth 3 -name requirements.txt | wc -l)
echo "    $REQS requirements.txt files under ten_packages/"

sudo env "PATH=$PATH" "UV_PYTHON=$PY_BIN" bash -c '
  set -e
  for req in "$1"/ten_packages/extension/*/requirements.txt \
             "$1"/ten_packages/system/*/requirements.txt; do
    [ -f "$req" ] || continue
    echo "    -> $(basename "$(dirname "$req")")"
    uv pip install --system -r "$req" -q
  done
' _ "$TENAPP"
ok "dependencies installed into $PY_BIN"

# ---------------------------------------------------------------- 4
echo "==> [4/6] Installing the frontend"
# This example runs the shared playground, not a per-example frontend. Without
# node_modules, `bun run dev` falls back to PATH and finds nmh's /usr/bin/next,
# which reports "Doesn't look like nmh is installed" and exits 1.
[[ -d "$PLAYGROUND" ]] || die "no playground at $PLAYGROUND"
cd "$PLAYGROUND"
bun install
[[ -x "$PLAYGROUND/node_modules/.bin/next" ]] \
  || die "node_modules/.bin/next missing after bun install"
ok "playground/node_modules/.bin/next present"

# ---------------------------------------------------------------- 5
echo "==> [5/6] Building the API server"
# task install's fourth step, and the only binary the playground talks to.
# Without it `task run` starts the frontend against a port nothing holds.
SERVER_DIR="$AI_AGENTS/server"
[[ -d "$SERVER_DIR" ]] || die "no server directory at $SERVER_DIR"
(cd "$SERVER_DIR" && go mod tidy && go mod download && go build -o bin/api main.go)
[[ -x "$SERVER_DIR/bin/api" ]] || die "server/bin/api missing after the build"
ok "server/bin/api: $(file -b "$SERVER_DIR/bin/api" 2>/dev/null | cut -d, -f1-2 || echo built)"

echo "==> [6/6] Verifying"
cd "$TENAPP"

# Reproduce what scripts/start.sh sets, then check the app resolves everything.
export LD_LIBRARY_PATH="$TENAPP/ten_packages/system/agora_rtc_sdk/lib:$TENAPP/ten_packages/system/ten_runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
UNRESOLVED=$(ldd -r bin/main 2>&1 | grep -E 'not found' || true)
if [[ -n "$UNRESOLVED" ]]; then
  echo "$UNRESOLVED" | sed 's/^/      /'
  die "bin/main has unresolved libraries"
fi
ok "bin/main resolves its libraries"

if "$PY_BIN" -c 'import pydantic, aiohttp, websockets' 2>/dev/null; then
  ok "core third-party deps import under $PY_BIN"
else
  die "pydantic/aiohttp/websockets do not import under $PY_BIN"
fi

echo
echo "==> Done. Start it with:"
echo "    tmux kill-session -t ten 2>/dev/null; sleep 1"
echo "    tmux new -d -s ten 'cd $AI_AGENTS/agents/examples/$EXAMPLE && task run 2>&1 | tee /tmp/task_run.log'"
