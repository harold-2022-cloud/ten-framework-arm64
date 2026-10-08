#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Get the board ready for a meeting: clear what a stuck run left behind, and
# make sure python3.12 imports what the meeting extensions import. Exits 0
# when it is ready. rerun_meeting_worker_check.sh and verify_meeting_board.sh
# run it first; alone, it is the way to unstick the board.
#
#   tools/ambarella/clear_meeting_worker.sh
#
#   - a check still running (check_meeting_*.py) is stopped
#   - the meeting worker is stopped through the Go server (/stop
#     meeting-room), and any meeting worker process still alive is killed
#   - nothing may answer on 8765
#   - a package the meeting extensions cannot import: their requirements.txt
#     go in with uv, as install_board_arm64.sh does it (sudo asks for a
#     password)
#
# Needs the Go server up (task run); if it does not answer, this says how to
# restart it.

set -uo pipefail

PY="${PY:-python3.12}"

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
EXTENSIONS="$REPO_ROOT/ai_agents/agents/ten_packages/extension"
ENV_FILE="$REPO_ROOT/ai_agents/.env"

port=$(grep -E '^SERVER_PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"'"'"' ')
SERVER="http://127.0.0.1:${port:-8081}"
UPLOADER="http://127.0.0.1:8765"
IMPORTS="import numpy, soundfile, sherpa_onnx, aiohttp, pydantic, opencc"

ok() { echo "  ok    $*"; }
fail() { echo "  FAIL  $*"; exit 1; }

# Processes whose command line matches, never this script or its parent.
pids_of() {
  pgrep -f "$1" | grep -vx -e "$$" -e "$PPID" || true
}

answers() {
  curl -s -m 3 -o /dev/null "$1"
}

checks=$(pids_of 'check_meeting_(worker|board|phone)\.py')
if [[ -n "$checks" ]]; then
  echo "  stopping a check still running: $(echo $checks)"
  kill $checks 2>/dev/null
  sleep 2
  ok "stopped the check that was still running"
fi

if ! answers "$SERVER/graphs"; then
  echo "  FAIL  the Go server does not answer at $SERVER"
  echo "        -> in the terminal running task run: Ctrl-C, then"
  echo "           cd $REPO_ROOT/ai_agents/agents/examples/voice-assistant && task run 2>&1 | tee /tmp/task_run.log"
  echo "           and run this again"
  exit 1
fi

reply=$(curl -s -m 10 -X POST "$SERVER/stop" -H 'Content-Type: application/json' \
  -d '{"request_id":"clear","channel_name":"meeting-room"}')
case "$reply" in
  *'"code":"0"'*) echo "  /stop meeting-room: stopped" ;;
  *) echo "  /stop meeting-room: none running" ;;
esac

# Workers the server no longer stops: a check's own, or one whose server was
# restarted. Each carries its property file, named for its channel, on its
# command line.
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
[[ -z "$(pids_of 'property-meeting')" ]] ||
  fail "meeting worker processes survive kill -9: $(echo $(pids_of 'property-meeting'))"

for _ in $(seq 15); do
  answers "$UPLOADER/meetings" || break
  sleep 1
done
answers "$UPLOADER/meetings" && fail "something still answers on 8765: ss -ltnp | grep 8765 says what"
ok "no meeting worker left; 8765 is free"

command -v "$PY" >/dev/null || fail "$PY is not on PATH (PY=... picks another)"
if "$PY" -c "$IMPORTS" 2>/dev/null; then
  ok "$PY imports everything the meeting extensions need"
  exit 0
fi
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
  ok "installed; $PY now imports everything the meeting extensions need"
  exit 0
fi
"$PY" -c "$IMPORTS" 2>&1 | tail -1 | sed 's/^/  /'
fail "$PY still cannot import what the meeting extensions need"
