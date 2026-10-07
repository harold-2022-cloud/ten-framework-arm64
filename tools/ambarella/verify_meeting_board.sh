#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Check the meeting-minutes work on the board in one run, and say at the end
# what held and what did not.
#
#   git pull --ff-only && tools/ambarella/verify_meeting_board.sh
#   tools/ambarella/verify_meeting_board.sh --minutes 12   # a shorter meeting
#   tools/ambarella/verify_meeting_board.sh --no-meeting   # steps 1-3 only
#
#   1 checkout   the commits under test are in this tree; stops here if not,
#                since everything after would test the old code
#   2 garbling   every answer probe_meeting_summary.py saved, mended by the
#                llm extension's utf8_mend.py: U+FFFD before and after
#   3 llm tests  ambarella_llm2_python's suite under the runtime's python3.12,
#                with the tenapp's own TEN runtime (no TEN_SYSTEM_DIR to type)
#   4 meeting    check_meeting_board.py on the AISHELL-4 meeting already on
#                the board, whole by default (about 35 minutes); then the
#                conclusion is searched for invented owners and U+FFFD
#
# Steps 2-4 run whatever happens to the one before; the summary lists each.
# Everything is also written to a log, named at the start and the end. Step 4
# needs the server up (task run) and the LLM daemon; keep the conversation
# graphs idle meanwhile -- the board's LLM serves one session at a time.

set -uo pipefail

PY="${PY:-python3.12}"
AUDIO="${AUDIO:-$HOME/meeting_probe/M_R003S01C01.wav}"
RTTM="${RTTM:-$HOME/meeting_probe/M_R003S01C01.rttm}"
SPEAKERS="${SPEAKERS:-6}"
MINUTES=""
MEETING=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minutes) MINUTES="$2"; shift 2 ;;
    --no-meeting) MEETING=0; shift ;;
    -h|--help) sed -n '6,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

LOG="/tmp/verify_meeting_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TOOLS="$REPO_ROOT/tools/ambarella"
LLM_EXT="$REPO_ROOT/ai_agents/agents/ten_packages/extension/ambarella_llm2_python"
SYSTEM_DIR="$REPO_ROOT/ai_agents/agents/examples/voice-assistant/tenapp/ten_packages/system"
PROBE_RUNS="$HOME/meeting_probe/summary_probe"
CHECK_RUNS="$HOME/meeting_probe/board_check"

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

echo "repo    $REPO_ROOT"
echo "log     $LOG"

# --- 1. the checkout carries the code under test -----------------------------
step "1. checkout"
git -C "$REPO_ROOT" log --oneline -1 || { failed "not a git checkout"; summary; exit 1; }
# Looked up in the whole history, without a pipe: a window of recent commits
# goes stale, and `git log | grep -q` dies of SIGPIPE under pipefail.
missing=0
for subject in \
  "count the orphans to mend a broken character" \
  "ask the conclusion for no owners at all" \
  "acknowledge segment_audio and transcribe at once"
do
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
  summary
  exit 1
fi
passed "checkout has the fixes under test"

# --- 2. the mender on every answer saved from the board ----------------------
step "2. garbling, from saved bytes"
runs=()
for dir in "$PROBE_RUNS"/*/; do
  compgen -G "$dir*.sse" >/dev/null && runs+=("${dir%/}")
done
if [[ ${#runs[@]} -eq 0 ]]; then
  noted "no saved answers under $PROBE_RUNS; skipped"
else
  before_total=0
  after_total=0
  for dir in "${runs[@]}"; do
    out=$("$PY" "$TOOLS/probe_meeting_summary.py" --garbling "$dir" 2>&1)
    echo "$out"
    # Table rows: answer, events, as read, joined, mended, events not UTF-8.
    # Rows saved by an older probe say "lost" -- their bytes are already
    # U+FFFD -- and are not counted.
    read -r before after < <(echo "$out" | awk \
      'NF == 6 && $2 ~ /^[0-9]+$/ && $5 ~ /^[0-9]+$/ {b += $3; a += $5}
       END {print b + 0, a + 0}')
    before_total=$((before_total + before))
    after_total=$((after_total + after))
  done
  if [[ $after_total -eq 0 ]]; then
    passed "garbling: $before_total U+FFFD as read before, 0 mended (${#runs[@]} saved runs)"
  else
    failed "garbling: $before_total U+FFFD as read before, $after_total left after mending"
  fi
fi

# --- 3. the llm extension's suite under the runtime's interpreter -----------
step "3. ambarella_llm2_python tests"
if [[ ! -d "$SYSTEM_DIR/ten_runtime_python" ]]; then
  failed "no TEN runtime at $SYSTEM_DIR; run ai_agents/agents/scripts/install_board_arm64.sh"
else
  out=$(TEN_SYSTEM_DIR="$SYSTEM_DIR" "$LLM_EXT/tests/bin/start" -q 2>&1)
  echo "$out" | tail -5
  count=""
  [[ "$out" =~ ([0-9]+)\ passed ]] && count="${BASH_REMATCH[1]}"
  if [[ -n "$count" ]] && ! [[ "$out" =~ [0-9]+\ (failed|error) ]]; then
    passed "llm extension: $count passed"
  else
    failed "llm extension tests did not pass; see above"
  fi
fi

# --- 4. a whole meeting through the graph ------------------------------------
if [[ $MEETING -eq 1 ]]; then
  step "4. meeting through the graph"
  args=(--audio "$AUDIO" --rttm "$RTTM" --speakers "$SPEAKERS")
  [[ -n "$MINUTES" ]] && args+=(--minutes "$MINUTES")
  before=$(ls -d "$CHECK_RUNS"/*/ 2>/dev/null | sort)
  "$PY" "$TOOLS/check_meeting_board.py" "${args[@]}"
  if [[ $? -eq 0 ]]; then
    passed "check_meeting_board.py: all its checks passed"
  else
    failed "check_meeting_board.py reported failures; see above"
  fi

  # Only this run's minutes: an earlier run's would answer for code it did
  # not run.
  after=$(ls -d "$CHECK_RUNS"/*/ 2>/dev/null | sort)
  latest=$(comm -13 <(echo "$before") <(echo "$after") | tail -1)
  minutes_txt="${latest%/}/minutes.txt"
  if [[ -n "$latest" && -f "$minutes_txt" ]]; then
    echo
    echo "  minutes: $minutes_txt"
    # The conclusion only: the transcript below it is what people said, and
    # says 老师 as often as they did.
    conclusion=$(awk '/^結論$/ {on = 1; next} /^話題 / {exit} on' "$minutes_txt")
    owners=$(grep -cE '负责人[：: ]*[0-9]' <<<"$conclusion")
    titles=$(grep -cE '老师|经理|主任|园长|校长|先生|女士' <<<"$conclusion")
    broken=$(grep -c $'\xef\xbf\xbd' "$minutes_txt")
    if [[ $owners -eq 0 ]]; then
      passed "conclusion: no numbered owners (负责人N)"
    else
      failed "conclusion: $owners line(s) with numbered owners (负责人N)"
    fi
    if [[ $broken -eq 0 ]]; then
      passed "minutes: no U+FFFD anywhere"
    else
      failed "minutes: $broken line(s) with U+FFFD"
      grep -n $'\xef\xbf\xbd' "$minutes_txt" | head -5
    fi
    if [[ $titles -gt 0 ]]; then
      noted "conclusion: $titles line(s) with a title such as 老师; read them -- the record names nobody"
    fi
    echo
    echo "  the conclusion, as written:"
    sed 's/^/    /' <<<"$conclusion"
  else
    failed "this run left no minutes.txt (the meeting did not complete)"
  fi
fi

summary
for line in "${RESULTS[@]}"; do
  [[ "$line" == FAIL* ]] && exit 1
done
exit 0
