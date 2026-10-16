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
#             screen's and its kiosk's (their last 2000 lines), and the
#             meeting worker's own lines from task_run.log (its last 5000)
#
#   tools/ambarella/pull_board_check.sh             today's
#   tools/ambarella/pull_board_check.sh 20261015    that day's
#
# Run it here, not on the board. They land in ambarella_ai_developer_kit_doc/
# board_check/ (git-ignored); what is there already is overwritten, nothing
# else is removed.
#
# Settings, from the environment: BOARD (lychee@192.168.0.19), DEST,
# BOARD_LOG_DIR (/tmp, where the board tools write their logs).

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
day=$1 logs=$2
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
if [ -r "$logs/task_run.log" ]; then
  grep -a '\[meeting_' "$logs/task_run.log" | tail -n 5000 > "$svc/task_run_meeting.log"
fi
set --
if [ -n "$runs" ]; then set -- "$@" -C ~/meeting_probe/board_check $runs; fi
if [ -n "$daylogs" ]; then set -- "$@" -C "$logs" $daylogs; fi
tar cf - "$@" -C "$out" "${day}_services"
BOARD

echo "from $BOARD, $DAY, into $DEST"
stream=$(mktemp)
trap 'rm -f "$stream"' EXIT
ssh "$BOARD" "bash -s -- $DAY $(printf %q "$LOGS")" <<< "$remote" > "$stream" || exit 3
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
ls "$DEST/${DAY}_services"/* 2>/dev/null | sed 's/^/  /' || echo "  none"
