#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Bring an Ambarella aarch64 board from a fresh checkout to a running voice
# assistant whose speech runs on the CPU.
#
#   ai_agents/agents/scripts/install_board_arm64.sh            # install, verify
#   ai_agents/agents/scripts/install_board_arm64.sh --run      # ... and start it
#   ai_agents/agents/scripts/install_board_arm64.sh --dry-run  # print the plan
#
#   --example NAME   which example to install   (default: voice-assistant)
#   --python X.Y     the interpreter the TEN runtime loads   (default: 3.12)
#   --force          redo steps already done
#
# This sequences the scripts that do the work rather than repeating them, so
# each stage is somewhere you can go and read:
#
#   1  setup_cpu_asr_tts_arm64.sh     the Zipformer ASR model and the Piper voice
#   2  task install                   the tenapp, then the prebuilt arm64 RTC
#                                     that tman install removes
#   3  setup_tts_runtime_deps.sh      the Python packages, for the interpreter
#                                     the runtime loads rather than the shell's
#   4  verify_asr_board.sh            the tests, against the real model
#
# Every stage is idempotent, so running this twice is not destructive and a
# failed run can be restarted without undoing anything.
#
# Not installed here, because it is not ours: the LLM. It is the vendor's
# daemon on 127.0.0.1:8080. tools/ambarella/check_llm_board.sh compares what
# the board has against what the kit's guide says it should.

set -uo pipefail

EXAMPLE_NAME="voice-assistant"
PY_VERSION="3.12"
DRY_RUN=0
FORCE=0
DO_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --example) EXAMPLE_NAME="$2"; shift 2 ;;
    --python)  PY_VERSION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force)   FORCE=1; shift ;;
    --run)     DO_RUN=1; shift ;;
    -h|--help) sed -n '6,34p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

LOG="/tmp/install_board_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
SCRIPTS="$REPO_ROOT/ai_agents/agents/scripts"
TOOLS="$REPO_ROOT/tools/ambarella"
EXAMPLE="$REPO_ROOT/ai_agents/agents/examples/$EXAMPLE_NAME"

die()  { printf '\nFATAL: %s\n' "$*" >&2; printf 'log: %s\n' "$LOG" >&2; exit 1; }
say()  { printf '\n\033[1m===== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
step() { printf '  ->    %s\n' "$*"; }

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  would run: %s\n' "$*"
    return 0
  fi
  "$@"
}

# ------------------------------------------------------------------ 0
say "0. Preflight"

arch=$(uname -m)
if [[ "$arch" != "aarch64" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    warn "this is $arch, not the aarch64 board; the plan below is printed"
    warn "for review only and several checks are skipped"
  else
    die "this installs onto the aarch64 board; here it is $arch"
  fi
else
  ok "aarch64"
fi

[[ -d "$EXAMPLE" ]] || die "no example at $EXAMPLE"
ok "example $EXAMPLE_NAME"

PY="python$PY_VERSION"
if command -v "$PY" >/dev/null 2>&1; then
  ok "$PY is $("$PY" -c 'import sys; print(sys.version.split()[0])')"
else
  [[ $DRY_RUN -eq 1 ]] || die "$PY is not on PATH.
       This is the interpreter the TEN runtime loads, and it is not
       necessarily the one \`python3\` points at -- on this board python3 is
       3.13 while the runtime loads 3.12. Install it, or pass --python X.Y."
  warn "$PY is not here; TEN_PYTHON_LIB_PATH cannot be resolved"
fi

# The runtime dlopens its interpreter rather than linking it, so the library
# cannot be read out of the binding's ELF -- which is why TEN_PYTHON_LIB_PATH
# exists at all. Ask the interpreter itself, with the finder CI uses.
FINDER="$REPO_ROOT/core/src/ten_runtime/binding/python/tools/find_libpython.py"
if [[ -z "${TEN_PYTHON_LIB_PATH:-}" ]] && command -v "$PY" >/dev/null 2>&1; then
  if [[ -f "$FINDER" ]]; then
    TEN_PYTHON_LIB_PATH=$("$PY" "$FINDER" 2>/dev/null | head -1)
  fi
fi
if [[ -n "${TEN_PYTHON_LIB_PATH:-}" ]]; then
  export TEN_PYTHON_LIB_PATH
  ok "TEN_PYTHON_LIB_PATH=$TEN_PYTHON_LIB_PATH"
else
  warn "TEN_PYTHON_LIB_PATH is unset and could not be resolved;"
  warn "stages 3 and 4 will say so rather than guess"
fi

command -v task >/dev/null 2>&1 || warn "task is not on PATH; stage 2 will fail"

# ------------------------------------------------------------------ 1
say "1. Speech models on the CPU"

CPU_SPEECH="$SCRIPTS/setup_cpu_asr_tts_arm64.sh"
[[ -x "$CPU_SPEECH" ]] || die "missing $CPU_SPEECH"
step "Zipformer ASR and the Piper voice (idempotent; skips what is present)"
CPU_ARGS=()
[[ $FORCE -eq 1 ]] && CPU_ARGS+=(--force)
run env VOICES=zh "$CPU_SPEECH" "${CPU_ARGS[@]}" ||
  die "the speech models could not be installed"

# ------------------------------------------------------------------ 2
say "2. The example's tenapp"

step "task install"
run bash -c "cd '$EXAMPLE' && task install" ||
  die "task install failed; its output is above and in $LOG"

# tman install rebuilds tenapp/ten_packages from the manifest and the arm64
# agora_rtc is not in the registry -- it was put there by hand. Left as is,
# the board joins no channel and nothing says why.
PREBUILT="$SCRIPTS/install_prebuilt_agora_rtc_arm64.sh"
TENAPP_EXT="$EXAMPLE/tenapp/ten_packages/extension"
if [[ -d "$TENAPP_EXT/agora_rtc" ]]; then
  ok "agora_rtc survived task install"
elif [[ -x "$PREBUILT" ]]; then
  step "restoring the prebuilt arm64 agora_rtc"
  run "$PREBUILT" "$EXAMPLE_NAME" || die "agora_rtc could not be restored"
else
  warn "agora_rtc is absent and $PREBUILT is not here; RTC will not work"
fi

# ------------------------------------------------------------------ 3
say "3. Python packages, where the runtime will look"

DEPS="$TOOLS/setup_tts_runtime_deps.sh"
[[ -x "$DEPS" ]] || die "missing $DEPS"
step "installing for ${TEN_PYTHON_LIB_PATH:-<unresolved>}, not for whatever python3 is"
run "$DEPS" --yes || die "the runtime's Python dependencies are not in place"

# ------------------------------------------------------------------ 4
say "4. Verify"

VERIFY="$TOOLS/verify_asr_board.sh"
if [[ -x "$VERIFY" ]]; then
  run "$VERIFY" || die "verification failed; the output above says which check"
else
  warn "no $VERIFY to run"
fi

if [[ -x "$TOOLS/check_llm_board.sh" ]]; then
  step "the LLM is the vendor's, so this reports rather than fixes"
  run "$TOOLS/check_llm_board.sh" || true
fi

# ------------------------------------------------------------------ 5
say "5. Run"

if [[ $DO_RUN -eq 1 ]]; then
  step "task run, logging to /tmp/task_run.log"
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  would run: cd %s && task run > /tmp/task_run.log 2>&1\n' "$EXAMPLE"
  else
    cd "$EXAMPLE" || die "cannot enter $EXAMPLE"
    task run > /tmp/task_run.log 2>&1 &
    printf '  started, pid %s; log /tmp/task_run.log\n' "$!"
    printf '  open the playground on :3000 and pick voice_assistant_sherpa_full\n'
  fi
else
  cat <<NEXT

  Installed. To start it:

    cd $EXAMPLE
    task run 2>&1 | tee /tmp/task_run.log

  Then open the playground on port 3000 and pick the graph
  voice_assistant_sherpa_full -- ASR and TTS on the CPU, the vendor's LLM
  on the VP.

  After a conversation, check what the ASR path did:

    $TOOLS/check_asr_log.sh

NEXT
fi

echo "  log: $LOG"
