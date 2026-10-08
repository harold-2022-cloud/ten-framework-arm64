#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Clear whatever a stuck meeting run left behind, make sure the packages the
# meeting extensions import are there, and run check_meeting_worker.py -- in
# one go, with a summary at the end.
#
#   git pull --ff-only && tools/ambarella/rerun_meeting_worker_check.sh
#   tools/ambarella/rerun_meeting_worker_check.sh --minutes 2   # passed on
#
#   1 checkout   the commits under test are in this tree; stops if not
#   2 clean up   clear_meeting_worker.sh: a check still running, the meeting
#                worker and any meeting worker process left are stopped,
#                8765 must be free, and missing packages are installed (sudo
#                asks for a password)
#   3 check      check_meeting_worker.py, with any arguments given here
#
# Needs the Go server up (task run). If it does not answer, this stops and
# says how to restart it. Everything is also written to a log, named at the
# start and the end.

set -uo pipefail

PY="${PY:-python3.12}"

LOG="/tmp/rerun_meeting_worker_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TOOLS="$REPO_ROOT/tools/ambarella"

RESULTS=()
step() { echo; echo "=== $* ==="; }
passed() { RESULTS+=("ok    $*"); echo "  ok    $*"; }
failed() { RESULTS+=("FAIL  $*"); echo "  FAIL  $*"; }
noted() { RESULTS+=("note  $*"); echo "  note  $*"; }

summary() {
  echo
  echo "=================== summary ==================="
  for line in "${RESULTS[@]}"; do echo "  $line"; done
  echo
  echo "log: $LOG"
}

finish() {
  summary
  for line in "${RESULTS[@]}"; do
    [[ "$line" == FAIL* ]] && exit 1
  done
  exit 0
}

echo "repo    $REPO_ROOT"
echo "log     $LOG"

# --- 1. the checkout carries the code under test -----------------------------
step "1. checkout"
missing=0
for subject in \
  "mark meetings interrupted only once the port is held" \
  "end a meeting failed whatever raises" \
  "stop the worker check where a meeting never starts"
do
  # Looked up in the whole history, without a pipe: `git log | grep -q`
  # dies of SIGPIPE under pipefail.
  found=$(git -C "$REPO_ROOT" log -1 --format=%h --fixed-strings \
    --grep="$subject" HEAD)
  if [[ -n "$found" ]]; then
    echo "  present: $found $subject"
  else
    echo "  MISSING: $subject"
    missing=1
  fi
done
if [[ $missing -eq 1 ]]; then
  failed "this checkout is behind: git -C $REPO_ROOT pull --ff-only"
  finish
fi
passed "checkout has the fixes under test"

# --- 2. clear what a stuck run left, and the packages ------------------------
step "2. clean up and packages"
if "$TOOLS/clear_meeting_worker.sh"; then
  passed "no meeting worker left; $PY imports what the meeting extensions need"
else
  failed "the board is not ready for a meeting; see above"
  finish
fi

# --- 3. the check --------------------------------------------------------------
step "3. check_meeting_worker.py"
"$PY" "$TOOLS/check_meeting_worker.py" "$@"
if [[ $? -eq 0 ]]; then
  passed "check_meeting_worker.py: all its checks passed"
else
  failed "check_meeting_worker.py reported failures; see above"
fi

finish
