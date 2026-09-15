#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Install everything sherpa_onnx_tts_python needs on the board, and leave it
# ready to run.
#
#   ai_agents/agents/scripts/install_sherpa_tts_board.sh
#   ai_agents/agents/scripts/install_sherpa_tts_board.sh --dry-run
#
# Nothing is written to the system Python path. The board runs a
# vendor-patched Fedora with no backup, and `uv pip install --system` -- what
# the example's own install script defaults to -- writes into the same
# site-packages rpm owns. Every pip call here is `--user`.
#
# Nothing here calls dnf.
#
#   --dry-run    print every step without running any of it
#   --force      redo a step that is already done
#   --skip-app   install the Python side only, not the example's tenapp

set -uo pipefail

DRY_RUN=0
FORCE=0
SKIP_APP=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=1 ;;
    --force)    FORCE=1 ;;
    --skip-app) SKIP_APP=1 ;;
    -h|--help)
      sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
EXAMPLE="$REPO_ROOT/ai_agents/agents/examples/voice-assistant"
EXT="$REPO_ROOT/ai_agents/agents/ten_packages/extension/sherpa_onnx_tts_python"
TTS_ROOT="${TTS_ROOT:-$HOME/piper_tts}"
VOICE_BUNDLE="${VOICE_BUNDLE:-vits-piper-zh_CN-huayan-medium}"
VOICE_DIR="$TTS_ROOT/vits/$VOICE_BUNDLE"

LOG="/tmp/sherpa_tts_install_$(date +%Y%m%d_%H%M%S).log"
if [[ $DRY_RUN -eq 0 ]]; then
  exec > >(tee "$LOG") 2>&1
fi

say()  { printf '\n\033[1m===== %s\033[0m\n' "$*"; }
step() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mFATAL\033[0m %s\n' "$*" >&2; exit 1; }

# Every mutating command goes through this, so --dry-run is honest rather
# than a second code path that can drift from the real one.
run() {
  # printf %q so an argument carrying spaces -- PIP_INSTALL_CMD is one --
  # shows its own quoting. "$*" would print it as three bare words and read
  # as a different command from the one that runs.
  local shown
  shown=$(printf '%q ' "$@")
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  would run: %s\n' "${shown% }"
    return 0
  fi
  step "${shown% }"
  "$@"
}

# ------------------------------------------------------------------ 0
say "0. Preflight"

arch=$(uname -m)
[[ "$arch" == "aarch64" ]] ||
  die "this is for the aarch64 board; here it is $arch"
ok "aarch64"

[[ -d "$EXT" ]] ||
  die "$EXT is missing -- pull the branch that carries the extension first"
ok "extension present"

command -v python3 >/dev/null || die "python3 is not on PATH"
py_version=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' ||
  die "python3 is $py_version; the extensions need 3.10 or newer"
ok "python3 $py_version at $(command -v python3)"

command -v pip3 >/dev/null || command -v pip >/dev/null ||
  die "neither pip3 nor pip is on PATH"
PIP=$(command -v pip3 || command -v pip)
ok "pip at $PIP"

# What a system-wide install would have overwritten, named rather than
# implied. This script does not do that, but the number is the reason.
if command -v rpm >/dev/null 2>&1; then
  owned=""
  for name in numpy scipy pydantic; do
    file=$(python3 -c "
import importlib
try:
    print(importlib.import_module('$name').__file__ or '')
except Exception:
    print('')
" 2>/dev/null)
    [[ -n "$file" ]] || continue
    if rpm -qf "$file" >/dev/null 2>&1; then
      owned="$owned $name($(rpm -qf "$file" 2>/dev/null))"
    fi
  done
  if [[ -n "$owned" ]]; then
    warn "rpm owns:$owned -- installing to --user so these are left alone"
  else
    ok "no rpm-owned python package among numpy, scipy, pydantic"
  fi
fi

# ------------------------------------------------------------------ 1
say "1. Python packages, into the user path only"

# sherpa-onnx publishes manylinux2014_aarch64 wheels, so this needs no
# toolchain. The rest are what the extension and its tests import.
PACKAGES=(
  "sherpa-onnx>=1.13.8"
  "numpy>=1.24.0"
  "scipy"
  "pydantic>=2"
  "pytest"
  "pytest-asyncio"
)

missing=()
for spec in "${PACKAGES[@]}"; do
  name="${spec%%[<>=]*}"
  module="${name//-/_}"
  if [[ $FORCE -eq 1 ]]; then
    missing+=("$spec")
  elif python3 -c "import $module" >/dev/null 2>&1; then
    ok "$name present"
  else
    missing+=("$spec")
  fi
done

if [[ ${#missing[@]} -eq 0 ]]; then
  ok "nothing to install"
else
  step "installing: ${missing[*]}"
  run "$PIP" install --user "${missing[@]}" ||
    die "pip install failed; nothing was written to the system path"
fi

if [[ $DRY_RUN -eq 0 ]]; then
  python3 -c '
import sherpa_onnx, site
print("  sherpa-onnx", getattr(sherpa_onnx, "__version__", "?"),
      "from", sherpa_onnx.__file__)
' || die "sherpa_onnx still does not import"
fi

# ------------------------------------------------------------------ 2
say "2. Voice"

if [[ -d "$VOICE_DIR" && $FORCE -eq 0 ]]; then
  ok "$VOICE_BUNDLE already unpacked at $VOICE_DIR"
else
  step "fetching $VOICE_BUNDLE through the CPU speech setup script"
  run env VOICES=zh TTS_ROOT="$TTS_ROOT" \
    "$REPO_ROOT/ai_agents/agents/scripts/setup_cpu_asr_tts_arm64.sh" ||
    die "voice fetch failed"
fi

if [[ $DRY_RUN -eq 0 ]]; then
  for required in "$VOICE_DIR"/*.onnx "$VOICE_DIR/tokens.txt"; do
    [[ -e "$required" ]] || die "$VOICE_BUNDLE is missing $(basename "$required")"
  done
  [[ -d "$VOICE_DIR/espeak-ng-data" ]] ||
    warn "$VOICE_BUNDLE has no espeak-ng-data; the config tolerates that"
  ok "$(du -sh "$VOICE_DIR" 2>/dev/null | cut -f1) in $VOICE_DIR"
fi

# ------------------------------------------------------------------ 3
say "3. The example's tenapp"

if [[ $SKIP_APP -eq 1 ]]; then
  warn "skipped by --skip-app"
elif ! command -v task >/dev/null 2>&1; then
  warn "task is not on PATH; skipping. Install it, then run:"
  warn "  cd $EXAMPLE && PIP_INSTALL_CMD='$PIP install --user' task install"
else
  # install_python_deps.sh defaults to `uv pip install --system`, which
  # writes into the rpm-owned site-packages. PIP_INSTALL_CMD is the hook it
  # reads; this is the whole reason the variable is set here.
  step "task install, with pip redirected to the user path"
  ( cd "$EXAMPLE" && run env PIP_INSTALL_CMD="$PIP install --user" task install ) ||
    die "task install failed"
fi

# ------------------------------------------------------------------ 4
say "4. Where the tests will find ten_ai_base"

# Order matters. A stale .ten/app left inside some other extension by an
# earlier standalone test also holds both packages, and picking it would run
# the tests against whatever version that install pinned. Ask for the tenapp
# this graph actually runs in first.
TEN_SYSTEM_DIR=""
CANDIDATES=(
  "$EXAMPLE/tenapp/ten_packages/system"
  "$REPO_ROOT/ai_agents/agents/ten_packages/system"
  "$EXT/.ten/app/ten_packages/system"
)
while IFS= read -r dir; do
  CANDIDATES+=("$dir")
done < <(find "$REPO_ROOT" -type d -name system 2>/dev/null | sort)

for dir in "${CANDIDATES[@]}"; do
  if [[ -d "$dir/ten_ai_base" && -d "$dir/ten_runtime_python" ]]; then
    TEN_SYSTEM_DIR="$dir"
    break
  fi
done

if [[ -z "$TEN_SYSTEM_DIR" ]]; then
  warn "no directory holds both ten_ai_base and ten_runtime_python yet;"
  warn "that is what task install produces, so finish step 3 first"
else
  ok "$TEN_SYSTEM_DIR"
fi

# ------------------------------------------------------------------ 5
say "5. Next"

cat <<NEXT

  Run the tests. The engine-contract ones need the voice, which is why they
  take it by environment rather than skipping quietly:

    cd $EXT
    TEN_SYSTEM_DIR=$TEN_SYSTEM_DIR \\
    SHERPA_ONNX_TTS_VOICE_DIR=$VOICE_DIR \\
        ./tests/bin/start

  Measure the voice, including time to the first audio:

    python3 $REPO_ROOT/tools/ambarella/probe_piper_tts.py

  Then bring the graph up and pick voice_assistant_sherpa_tts:

    cd $EXAMPLE
    task run 2>&1 | tee /tmp/task_run.log

NEXT

if [[ $DRY_RUN -eq 0 ]]; then
  echo "  log: $LOG"
fi
exit 0
