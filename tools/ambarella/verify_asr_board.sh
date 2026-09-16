#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Run sherpa_onnx_asr_python's tests on the board, under the interpreter the
# TEN runtime actually loads, against the real model.
#
#   tools/ambarella/verify_asr_board.sh
#   tools/ambarella/verify_asr_board.sh --model-dir /path/to/bundle
#
# Touches no LLM, starts nothing, stops nothing. Safe to run with the voice
# assistant up, though the board's four cores are shared so the timings will
# be worse than with it stopped.
#
# Three traps this exists to close, each of which has already cost a day:
#
#   The board's shell python3 is 3.13 and the runtime embeds 3.12. A suite
#   that passes under one says nothing about the other.
#
#   Without SHERPA_ONNX_ASR_MODEL_DIR the six engine-contract tests skip and
#   pytest still exits 0, which reads as success and verified nothing.
#
#   A checkout that is behind reports the old code passing. The commits under
#   test are named below and their absence is fatal.

set -uo pipefail

MODEL_DIR="${SHERPA_ONNX_ASR_MODEL_DIR:-}"
DEFAULT_MODEL_DIR=/home/lychee/zipformer_asr/models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20
TASK_LOG="${TASK_LOG:-/tmp/task_run.log}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model-dir) MODEL_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '6,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

LOG="/tmp/verify_asr_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ASR_EXT="$REPO_ROOT/ai_agents/agents/ten_packages/extension/sherpa_onnx_asr_python"
MAIN_EXT="$REPO_ROOT/ai_agents/agents/examples/voice-assistant/tenapp/ten_packages/extension/main_python"

fail() { echo; echo "FAILED: $*"; echo "log: $LOG"; exit 1; }
step() { echo; echo "=== $* ==="; }

echo "repo    $REPO_ROOT"
echo "log     $LOG"

# --- 1. the checkout carries the code under test -----------------------------
step "checkout"
git -C "$REPO_ROOT" log --oneline -1 || fail "not a git checkout"
if ! git -C "$REPO_ROOT" diff --quiet || ! git -C "$REPO_ROOT" diff --cached --quiet; then
  echo "NOTE: the working tree has uncommitted changes; what runs below is"
  echo "      the tree, not the commit printed above."
fi

# Read the log once and match in the shell. A `git log | grep -q` pipeline
# reports 141 under pipefail -- grep exits on the first match and git dies of
# SIGPIPE -- so every commit would look missing however many are there.
SUBJECTS=$(git -C "$REPO_ROOT" log --oneline -40)
for subject in \
  "keep the partial fallback clear of the ASR's own final" \
  "write the input audio when dump is on" \
  "say which silence ended an utterance"
do
  if [[ "$SUBJECTS" == *"$subject"* ]]; then
    echo "  present: $subject"
  else
    fail "this checkout is behind; missing commit: $subject
       git -C $REPO_ROOT pull arm64 feat/arm64-native-build"
  fi
done

# --- 2. the interpreter the runtime loads ------------------------------------
step "interpreter"
LIB="${TEN_PYTHON_LIB_PATH:-}"
SOURCE="TEN_PYTHON_LIB_PATH"
if [[ -z "$LIB" ]]; then
  # The log is colourised, and [^ ]* happily swallows the escape that
  # follows the filename. Strip the colour first, then match narrowly.
  LIB=$(sed 's/\x1b\[[0-9;]*m//g' "$TASK_LOG" 2>/dev/null |
    grep -oE 'libpython3\.[0-9]+\.so[0-9.]*' | tail -1)
  SOURCE="$TASK_LOG"
fi
if [[ -z "$LIB" ]]; then
  fail "cannot tell which interpreter the runtime uses.
       Either export TEN_PYTHON_LIB_PATH, or run the app once so that
       $TASK_LOG records the libpython it loaded, then run this again."
fi
VERSION=$(basename "$LIB" | grep -oE '3\.[0-9]+')
PY="python$VERSION"
command -v "$PY" >/dev/null 2>&1 ||
  fail "the runtime loads $LIB (from $SOURCE) but $PY is not on PATH.
       Installing for a different python is the trap this check exists for."
echo "  runtime loads $LIB  (from $SOURCE)"
echo "  testing under $("$PY" -c 'import sys; print(sys.executable, sys.version.split()[0])')"
export TEN_PYTHON_LIB_PATH="$LIB"

# --- 3. the model, without which the real tests silently skip ----------------
step "model"
[[ -n "$MODEL_DIR" ]] || MODEL_DIR="$DEFAULT_MODEL_DIR"
[[ -d "$MODEL_DIR" ]] ||
  fail "no model bundle at $MODEL_DIR
       Pass --model-dir, or export SHERPA_ONNX_ASR_MODEL_DIR.
       Without it the engine-contract tests skip and the run still exits 0."
for pattern in 'encoder-*.onnx' 'decoder-*.onnx' 'joiner-*.onnx' 'tokens.txt'; do
  # shellcheck disable=SC2086
  compgen -G "$MODEL_DIR/"$pattern >/dev/null ||
    fail "the bundle at $MODEL_DIR has no $pattern"
done
echo "  $MODEL_DIR"
export SHERPA_ONNX_ASR_MODEL_DIR="$MODEL_DIR"

# --- 3b. where the runtime and the base classes actually are -----------------
step "system packages"
# Not a fixed path. These are registry dependencies that tman materialises,
# so a machine that installed the example has them under its tenapp while a
# plain checkout has them under agents/. Guessing one produced
# "No module named ten_runtime" on the board with the directory sitting
# somewhere else in the same tree.
CANDIDATES=(
  "${TEN_SYSTEM_DIR:-}"
  "$REPO_ROOT/ai_agents/agents/examples/voice-assistant/tenapp/ten_packages/system"
  "$REPO_ROOT/ai_agents/agents/ten_packages/system"
  "$ASR_EXT/.ten/app/ten_packages/system"
)
SYSTEM_DIR=""
for candidate in "${CANDIDATES[@]}"; do
  [[ -n "$candidate" ]] || continue
  if [[ -d "$candidate/ten_runtime_python/interface" &&
        -d "$candidate/ten_ai_base/interface" ]]; then
    SYSTEM_DIR="$candidate"
    break
  fi
done
if [[ -z "$SYSTEM_DIR" ]]; then
  while IFS= read -r hit; do
    parent=$(dirname "$hit")
    if [[ -d "$parent/ten_ai_base/interface" ]]; then
      SYSTEM_DIR="$parent"
      break
    fi
  done < <(find "$REPO_ROOT" -type d -name ten_runtime_python 2>/dev/null)
fi
if [[ -z "$SYSTEM_DIR" ]]; then
  echo "  looked in:"
  for candidate in "${CANDIDATES[@]}"; do
    [[ -n "$candidate" ]] && echo "    $candidate"
  done
  fail "no directory holds both ten_runtime_python/interface and
       ten_ai_base/interface. They are registry dependencies rather than
       source, so they exist only where tman put them:
         cd $REPO_ROOT/ai_agents/agents/examples/voice-assistant && task install"
fi
echo "  $SYSTEM_DIR"
export TEN_SYSTEM_DIR="$SYSTEM_DIR"

# --- 4. the whole suite, under that interpreter, with nothing skipped --------
step "sherpa_onnx_asr_python, full suite"
# One run. The runner hands pytest tests/ itself, so naming a file here adds
# to that rather than narrowing it -- the first version of this script ran the
# whole suite twice and called the first time "engine contract".
SUITE_OUT=$("$ASR_EXT/tests/bin/start" -v 2>&1)
echo "$SUITE_OUT" | tail -8
# Matched in the shell, not through a pipe: `grep -q` exits on its first match
# and the writer dies of SIGPIPE, which under pipefail reports 141 and would
# silently disable the check this script exists for.
if [[ "$SUITE_OUT" =~ ([0-9]+)\ skipped ]]; then
  fail "${BASH_REMATCH[1]} tests skipped. The ones that put real audio through
       the real model skip when SHERPA_ONNX_ASR_MODEL_DIR is unset, and pytest
       still exits 0, so a run with skips proves less than it looks like."
fi
[[ "$SUITE_OUT" =~ [0-9]+\ passed ]] || fail "the ASR suite did not pass"

REAL_MODEL=$(echo "$SUITE_OUT" | grep -c "test_engine_contract.*PASSED")
[[ "$REAL_MODEL" -gt 0 ]] ||
  fail "no test_engine_contract case ran. Those are the only ones that touch
       the model on this board; without them the run says nothing about it."
echo "  cases that ran against the real model: $REAL_MODEL"

# --- 6. main_control, where the commit-delay change lives --------------------
step "main_python, full suite"
if [[ -x "$MAIN_EXT/tests/bin/start" ]]; then
  "$MAIN_EXT/tests/bin/start" -q 2>&1 | tail -6 ||
    fail "the main_python suite did not pass"
else
  echo "  no runner at $MAIN_EXT/tests/bin/start -- skipped"
fi

# --- 7. what the graph will do with the new value ---------------------------
step "the value under test, as the graph will read it"
grep -n "ASR_PARTIAL_COMMIT_DELAY_SECONDS = " "$MAIN_EXT/extension.py"
PROPERTY="$REPO_ROOT/ai_agents/agents/examples/voice-assistant/tenapp/property.json"
grep -n "rule2_min_trailing_silence" "$PROPERTY" | head -2
echo "  dump keys in the graph (none on stt means the capture stays off):"
DUMPS=$(grep -n '"dump"' "$PROPERTY")
[[ -n "$DUMPS" ]] && echo "$DUMPS" || echo "    none on any node"

echo
echo "PASSED. log: $LOG"
