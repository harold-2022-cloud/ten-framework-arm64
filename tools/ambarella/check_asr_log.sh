#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Read a task_run.log and say whether the ASR path behaved.
#
#   tools/ambarella/check_asr_log.sh [/tmp/task_run.log]
#
# Reads a file. Changes nothing, needs no board.
#
# Each check is a thing that was wrong in a previous run, so a pass here means
# that particular failure did not come back:
#
#   the stand-in final pre-empting the engine's own       (11:41 run: fixed)
#   a line for every 2.56 s of silence, 64 of 67          (11:41 run: present)
#   the backlog warning latching on a constant offset     (11:41 run: present)

set -uo pipefail

LOG="${1:-/tmp/task_run.log}"
[[ -r "$LOG" ]] || { echo "cannot read $LOG" >&2; exit 2; }

CLEAN=$(mktemp)
trap 'rm -f "$CLEAN"' EXIT
sed 's/\x1b\[[0-9;]*m//g' "$LOG" > "$CLEAN"

count() { grep -cE "$1" "$CLEAN" || true; }

SPOKEN=$(grep -E "endpoint after" "$CLEAN" | grep -vc "text=''" || true)
SILENT=$(grep -E "endpoint after" "$CLEAN" | grep -c "text=''" || true)
COMMITS=$(count "commit stable partial as final")
STARTS=$(count "decoding starts .* behind the audio")
FALLING=$(count "falling behind")
CAUGHT=$(count "caught up")
OLD_LAG=$(count "decoder [0-9]+ ms behind the audio")
FINALS=$(grep -c "'final': True" "$CLEAN" || true)

echo "log      $LOG"
echo
printf '%-42s %s\n' "utterances that reached an endpoint" "$SPOKEN"
printf '%-42s %s\n' "ASR finals sent" "$FINALS"
echo

status=0
check() { # name, actual, expected-description, ok?
  if [[ "$4" == "1" ]]; then
    printf '  \033[32mOK  \033[0m %-46s %s\n' "$1" "$2"
  else
    printf '  \033[31mBAD \033[0m %-46s %s   (want %s)\n' "$1" "$2" "$3"
    status=1
  fi
}

check "stand-in finals (want none)" "$COMMITS" "0" \
  "$([[ "$COMMITS" -eq 0 ]] && echo 1 || echo 0)"
check "silent endpoints logged (want none)" "$SILENT" "0" \
  "$([[ "$SILENT" -eq 0 ]] && echo 1 || echo 0)"
check "backlog reported once at the start" "$STARTS" "1" \
  "$([[ "$STARTS" -eq 1 ]] && echo 1 || echo 0)"
check "old absolute-lag lines (want none)" "$OLD_LAG" "0" \
  "$([[ "$OLD_LAG" -eq 0 ]] && echo 1 || echo 0)"
check "a line per spoken utterance" "$SPOKEN" "= finals" \
  "$([[ "$SPOKEN" -gt 0 && "$SPOKEN" -eq "$FINALS" ]] && echo 1 || echo 0)"

if [[ "$FALLING" -gt 0 ]]; then
  echo
  echo "  the decoder lost ground $FALLING time(s), recovered $CAUGHT:"
  grep -E "falling behind|caught up" "$CLEAN" | sed 's/^/    /' | cut -c1-140
fi

echo
echo "what the engine heard, and what ended each utterance:"
# No cut: these lines end in what was said, and cut counts bytes, so it
# leaves a half character behind on anything but ASCII.
grep -E "endpoint after" "$CLEAN" | grep -v "text=''" |
  sed -E "s/^.*T([0-9:]+\.[0-9]{3})[0-9+:]* .*\[stt\] /    \1  /"
[[ "$SPOKEN" -eq 0 ]] && echo "    (nobody spoke, or the graph was not the sherpa one)"

echo
if [[ "$status" -eq 0 ]]; then
  echo "PASSED"
else
  echo "FAILED -- a checkout without the fixes, or a failure that came back."
  echo "         git pull --ff-only, restart task run,"
  echo "         and note that a log from before the restart still has the old"
  echo "         lines in it."
fi
exit "$status"
