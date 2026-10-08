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
#   2 clean up   a check still running is stopped; the meeting worker is
#                stopped through the Go server (/stop meeting-room), and any
#                meeting worker process still alive after that is killed;
#                then nothing may answer on 8765
#   3 packages   what the meeting extensions import, under python3.12. If one
#                is missing, the meeting extensions' requirements.txt go in
#                with uv, as install_board_arm64.sh does it (sudo asks for a
#                password)
#   4 check      check_meeting_worker.py, with any arguments given here
#
# Needs the Go server up (task run). If it does not answer, this stops and
# says how to restart it. Everything is also written to a log, named at the
# start and the end.

set -uo pipefail

PY="${PY:-python3.12}"
SERVER_PORT_DEFAULT=8081

LOG="/tmp/rerun_meeting_worker_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TOOLS="$REPO_ROOT/tools/ambarella"
EXTENSIONS="$REPO_ROOT/ai_agents/agents/ten_packages/extension"
ENV_FILE="$REPO_ROOT/ai_agents/.env"

port=$(grep -E '^SERVER_PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"'"'"' ')
SERVER="http://127.0.0.1:${port:-$SERVER_PORT_DEFAULT}"
UPLOADER="http://127.0.0.1:8765"

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

# Processes whose command line matches, never this script or its parent.
pids_of() {
  pgrep -f "$1" | grep -vx -e "$$" -e "$PPID" || true
}

answers() {
  curl -s -m 3 -o /dev/null "$1"
}

echo "repo    $REPO_ROOT"
echo "server  $SERVER"
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

# --- 2. clear what a stuck run left ------------------------------------------
step "2. clean up"
checks=$(pids_of 'check_meeting_(worker|board)\.py')
if [[ -n "$checks" ]]; then
  echo "  stopping a check still running: $(echo $checks)"
  kill $checks 2>/dev/null
  sleep 2
  passed "stopped the check that was still running"
else
  echo "  no check running"
fi

if ! answers "$SERVER/graphs"; then
  failed "the Go server does not answer at $SERVER"
  echo "        -> in the terminal running task run: Ctrl-C, then"
  echo "           cd $REPO_ROOT/ai_agents/agents/examples/voice-assistant && task run 2>&1 | tee /tmp/task_run.log"
  echo "           and run this again"
  finish
fi

reply=$(curl -s -m 10 -X POST "$SERVER/stop" -H 'Content-Type: application/json' \
  -d '{"request_id":"rerun","channel_name":"meeting-room"}')
case "$reply" in
  *'"code":"0"'*) echo "  /stop meeting-room: stopped" ;;
  *) echo "  /stop meeting-room: $reply (none running is fine)" ;;
esac

# Workers the server no longer stops: the check's own intruders, or one
# whose server was restarted. Each carries its property file, named for its
# channel, on its command line.
for _ in 1 2 3 4 5; do
  [[ -z "$(pids_of 'property-meeting')" ]] && break
  sleep 1
done
left=$(pids_of 'property-meeting')
if [[ -n "$left" ]]; then
  echo "  meeting worker processes still alive: $(echo $left)"
  kill $left 2>/dev/null
  sleep 3
  left=$(pids_of 'property-meeting')
  [[ -n "$left" ]] && kill -9 $left 2>/dev/null && sleep 1
fi
if [[ -n "$(pids_of 'property-meeting')" ]]; then
  failed "meeting worker processes survive kill -9: $(echo $(pids_of 'property-meeting'))"
  finish
fi

for _ in $(seq 15); do
  answers "$UPLOADER/meetings" || break
  sleep 1
done
if answers "$UPLOADER/meetings"; then
  failed "something still answers on 8765: ss -ltnp | grep 8765 says what"
  finish
fi
passed "no meeting worker left; 8765 is free"

# --- 3. what the meeting extensions import -----------------------------------
step "3. packages under $PY"
IMPORTS="import numpy, soundfile, sherpa_onnx, aiohttp, pydantic, opencc"
if ! command -v "$PY" >/dev/null; then
  failed "$PY is not on PATH (PY=... picks another)"
  finish
fi
if "$PY" -c "$IMPORTS" 2>/dev/null; then
  passed "$PY imports everything the meeting extensions need"
else
  "$PY" -c "$IMPORTS" 2>&1 | tail -1 | sed 's/^/  /'
  echo "  installing the meeting extensions' requirements.txt into $PY"
  reqs=()
  for ext in meeting_control_python meeting_uploader meeting_segmenter meeting_transcriber; do
    [[ -f "$EXTENSIONS/$ext/requirements.txt" ]] && reqs+=(-r "$EXTENSIONS/$ext/requirements.txt")
  done
  # /usr/local/lib*/python3.12/site-packages needs root, as in
  # finish_example_install_arm64.sh; UV_PYTHON is passed because sudo drops it.
  elevate=(sudo env "PATH=$PATH")
  [[ $(id -u) -eq 0 ]] && elevate=(env)
  "${elevate[@]}" "UV_PYTHON=$(command -v "$PY")" uv pip install --system -q "${reqs[@]}"
  if "$PY" -c "$IMPORTS" 2>/dev/null; then
    passed "installed; $PY now imports everything the meeting extensions need"
  else
    "$PY" -c "$IMPORTS" 2>&1 | tail -1 | sed 's/^/  /'
    failed "$PY still cannot import what the meeting extensions need"
    finish
  fi
fi

# --- 4. the check --------------------------------------------------------------
step "4. check_meeting_worker.py"
"$PY" "$TOOLS/check_meeting_worker.py" "$@"
if [[ $? -eq 0 ]]; then
  passed "check_meeting_worker.py: all its checks passed"
else
  failed "check_meeting_worker.py reported failures; see above"
fi

finish
