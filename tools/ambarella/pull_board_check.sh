#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Bring a day's meeting checks back from the board to this machine: every
# run check_meeting_board.py left in ~/meeting_probe/board_check that day,
# and verify_meeting_board.sh's whole log of that day from the board's /tmp
# (memory, the summary). One SSH connection, so the password is asked once.
#
#   tools/ambarella/pull_board_check.sh             today's
#   tools/ambarella/pull_board_check.sh 20261015    that day's
#
# Run it here, not on the board. They land in ambarella_ai_developer_kit_doc/
# board_check/ (git-ignored); what is there already is overwritten, nothing
# else is removed.
#
# Settings, from the environment: BOARD (lychee@192.168.0.19), DEST,
# BOARD_LOG_DIR (/tmp, where verify_meeting_board.sh writes its log).

set -euo pipefail

DAY="${1:-$(date +%Y%m%d)}"
[[ "$DAY" =~ ^[0-9]{8}$ ]] || { echo "usage: $0 [YYYYMMDD]" >&2; exit 2; }
BOARD="${BOARD:-lychee@192.168.0.19}"
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DEST="${DEST:-$REPO_ROOT/ambarella_ai_developer_kit_doc/board_check}"
LOGS="${BOARD_LOG_DIR:-/tmp}"

mkdir -p "$DEST"
# On the board: the day's runs and logs as one tar stream, or a word on
# stderr and exit 3 when there are none.
remote="cd ~/meeting_probe/board_check 2>/dev/null || { echo 'no ~/meeting_probe/board_check on the board' >&2; exit 3; }
runs=\$(ls -d ${DAY}-*/ 2>/dev/null | tr -d /) || true
logs=\$(cd $(printf %q "$LOGS") 2>/dev/null && ls verify_meeting_${DAY}_*.log 2>/dev/null) || true
[ -n \"\$runs\$logs\" ] || { echo 'nothing from $DAY on the board' >&2; exit 3; }
tar cf - \$runs \${logs:+-C $(printf %q "$LOGS") \$logs}"

echo "from $BOARD, $DAY, into $DEST"
stream=$(mktemp)
trap 'rm -f "$stream"' EXIT
ssh "$BOARD" "$remote" > "$stream" || exit 3
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
ls "$DEST"/verify_meeting_"$DAY"_*.log 2>/dev/null | sed 's/^/  /' || echo "  none"
