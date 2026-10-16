#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Bring a day's checks back from the board to this machine, over one SSH
# connection, so the password is asked once:
#
#   runs      every run check_meeting_board.py left in ~/meeting_probe/
#             board_check that day (record, minutes, the upload)
#   logs      that day's log of each board tool, from the board's /tmp:
#             verify_meeting_board.sh, prepare_meeting_demo.sh,
#             install_board_services.sh, probe_demo_screen.py
#   services  the service logs as they are now, in <day>_services/: the demo
#             screen's and its kiosk's (their last 2000 lines), the API
#             server's task_run.log (its last 5000), each meeting worker's own
#             log of that day or after from the server's LOG_PATH (workers/,
#             their last 20000), and every meeting on the board with its
#             state (meetings.txt)
#
#   tools/ambarella/pull_board_check.sh             today's
#   tools/ambarella/pull_board_check.sh 20261015    that day's
#
# Run it here, not on the board. They land in ambarella_ai_developer_kit_doc/
# board_check/ (git-ignored); what is there already is overwritten, nothing
# else is removed.
#
# Settings, from the environment: BOARD (lychee@192.168.0.19), DEST,
# BOARD_LOG_DIR (/tmp, where the board tools write their logs), BOARD_REPO
# (the board's checkout, ~/ten-framework, whose ai_agents/.env names LOG_PATH
# and MEETINGS_DIR).

set -euo pipefail

DAY="${1:-$(date +%Y%m%d)}"
[[ "$DAY" =~ ^[0-9]{8}$ ]] || { echo "usage: $0 [YYYYMMDD]" >&2; exit 2; }
BOARD="${BOARD:-lychee@192.168.0.19}"
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DEST="${DEST:-$REPO_ROOT/ambarella_ai_developer_kit_doc/board_check}"
LOGS="${BOARD_LOG_DIR:-/tmp}"

mkdir -p "$DEST"
# Run on the board, fed on stdin: the day's runs, logs and the service logs
# as one tar stream, or a word on stderr and exit 3 when the day left none.
read -r -d '' remote <<'BOARD' || true
set -u
day=$1 logs=$2 repo=${3:-$HOME/ten-framework}
setting() { grep -E "^$1=" "$repo/ai_agents/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'"; }
logpath=$(setting LOG_PATH)
logpath=${logpath:-/tmp/ten_agent}
meetings=$(setting MEETINGS_DIR)
meetings=${meetings:-/tmp/meetings}
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
runs=$(cd ~/meeting_probe/board_check 2>/dev/null && ls -d "$day"-*/ 2>/dev/null | tr -d /) || true
daylogs=$(cd "$logs" 2>/dev/null && ls verify_meeting_"$day"_*.log prepare_meeting_demo_"$day"_*.log \
  install_board_services_"$day"_*.log probe_demo_screen_"$day"_*.log 2>/dev/null) || true
[ -n "$runs$daylogs" ] || { echo "nothing from $day on the board" >&2; exit 3; }
svc="$out/${day}_services"
mkdir -p "$svc"
for f in meeting_screen.log meeting_screen_kiosk.log; do
  if [ -r "$logs/$f" ]; then tail -n 2000 "$logs/$f" > "$svc/$f"; fi
done
if [ -r "$logs/task_run.log" ]; then tail -n 5000 "$logs/task_run.log" > "$svc/task_run.log"; fi
# A worker's own log, when the server keeps one per worker (LOG_STDOUT false):
# the day's, and any still written to since.
mkdir -p "$svc/workers"
for f in "$logpath"/app-*.log; do
  [ -f "$f" ] || continue
  [ "$(date -r "$f" +%Y%m%d)" -ge "$day" ] || continue
  tail -n 20000 "$f" > "$svc/workers/$(basename "$f")"
done
python3 -I - "$meetings" > "$svc/meetings.txt" <<'PY'
import json, os, sys, time
root = sys.argv[1]
print(f"meetings in {root}, as they are now:")
def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None
try:
    names = sorted(os.listdir(root))
except OSError as err:
    names = []
    print(f"  cannot list it: {err}")
for name in names:
    folder = os.path.join(root, name)
    if not os.path.isdir(folder):
        continue
    state = load(os.path.join(folder, "work", "state.json"))
    if state is None:
        print(f"{name}  (no state)")
        continue
    seconds = int((load(os.path.join(folder, "upload.json")) or {}).get("duration_s") or 0)
    try:
        size = os.path.getsize(os.path.join(folder, "audio.ogg"))
    except OSError:
        size = 0
    received = state.get("received_at")
    when = "-"
    if isinstance(received, (int, float)):
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(received))
    print(f"{name}  {state.get('state')}  {seconds // 60:02d}:{seconds % 60:02d}  "
          f"{size} bytes  {state.get('error') or '-'}  received {when}")
PY
set --
if [ -n "$runs" ]; then set -- "$@" -C ~/meeting_probe/board_check $runs; fi
if [ -n "$daylogs" ]; then set -- "$@" -C "$logs" $daylogs; fi
tar cf - "$@" -C "$out" "${day}_services"
BOARD

echo "from $BOARD, $DAY, into $DEST"
stream=$(mktemp)
trap 'rm -f "$stream"' EXIT
ssh "$BOARD" "bash -s -- $DAY $(printf %q "$LOGS") $(printf %q "${BOARD_REPO:-}")" <<< "$remote" > "$stream" || exit 3
tar xf "$stream" -C "$DEST"

echo "runs:"
found=0
for d in "$DEST/$DAY"-*/; do
  [[ -d "$d" ]] || continue
  found=1
  echo "  ${d%/}  ($(ls "$d" | tr '\n' ' '))"
done
((found)) || echo "  none"
echo "logs:"
ls "$DEST"/{verify_meeting,prepare_meeting_demo,install_board_services,probe_demo_screen}_"$DAY"_*.log \
  2>/dev/null | sed 's/^/  /' || echo "  none"
echo "services, as they are now:"
(cd "$DEST" && find "${DAY}_services" -type f | sort) 2>/dev/null | sed 's/^/  /' || echo "  none"
