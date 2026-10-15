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
#   tools/ambarella/verify_meeting_board.sh --script traditional
#   tools/ambarella/verify_meeting_board.sh --script traditional --minutes 12 --phone
#   tools/ambarella/verify_meeting_board.sh --graph NAME   # another meeting graph
#   ... --keep-qwen   # if it switched the board from the 7B, Qwen3.5-4B left serving
#
#   1 checkout   the commits under test are in this tree; stops here if not,
#                since everything after would test the old code
#   2 garbling   every answer probe_meeting_summary.py saved, mended by the
#                llm extension's utf8_mend.py: U+FFFD before and after.
#                For a graph on Qwen3.5-4B, instead: the model's chat template
#                brought up to date (vLLM restarted if it changed), Qwen3.5-4B
#                started if it is not running (a meeting worker left up is
#                cleared first), and serve_qwen35.sh test
#   3 llm tests  the graph's llm extension's suite -- ambarella_llm2_python,
#                or openai_llm2_python for Qwen3.5-4B -- under the runtime's
#                python3.12, with the tenapp's own TEN runtime (no
#                TEN_SYSTEM_DIR to type)
#   4 meeting    clear_meeting_worker.sh first (a stuck run, missing
#                packages), then check_meeting_board.py on the AISHELL-4
#                meeting already on the board, whole by default (about 35
#                minutes); with --script, every text in the record must be
#                in that script; then the conclusion is searched for invented
#                owners and U+FFFD
#   5 phone      with --phone: clear_meeting_worker.sh again, then
#                check_meeting_phone.py waits for a meeting from the Android
#                app, follows it, and checks the record (in --script, or
#                Traditional, the app's default) and that the app left the
#                worker running
#
# Steps 2-5 run whatever happens to the one before; the summary lists each.
# Everything is also written to a log, named at the start and the end. Step 4
# needs the server up (task run) and the graph's LLM; keep the conversation
# graphs idle meanwhile -- the board's LLM serves one session at a time. For a
# graph on Qwen3.5-4B, sudo is asked at the start (switching the LLM needs it),
# and the board is left on the LLM it served: if this run switched it from
# the 7B, the 7B is given back at the end -- also after Ctrl-C -- and checked
# (its daemon runs, 8080 listens), unless --keep-qwen.

set -uo pipefail

PY="${PY:-python3.12}"
AUDIO="${AUDIO:-$HOME/meeting_probe/M_R003S01C01.wav}"
RTTM="${RTTM:-$HOME/meeting_probe/M_R003S01C01.rttm}"
SPEAKERS="${SPEAKERS:-6}"
MINUTES=""
SCRIPT=""
MEETING=1
PHONE=0
GRAPH="meeting_minutes"
KEEP_QWEN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minutes) MINUTES="$2"; shift 2 ;;
    --script) SCRIPT="$2"; shift 2 ;;
    --no-meeting) MEETING=0; shift ;;
    --phone) PHONE=1; shift ;;
    --graph) GRAPH="$2"; shift 2 ;;
    --keep-qwen) KEEP_QWEN=1; shift ;;
    -h|--help) sed -n '6,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

LOG="${LOG_DIR:-/tmp}/verify_meeting_$(date +%Y%m%d_%H%M%S).log"
# tee ignores Ctrl-C and a hangup: they reach the whole foreground group, and
# a tee gone with them would take on_exit's give-back down with SIGPIPE.
exec > >(trap '' INT HUP; exec tee "$LOG") 2>&1
TEE_PID=$!

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TOOLS="$REPO_ROOT/tools/ambarella"
LLM_EXT="$REPO_ROOT/ai_agents/agents/ten_packages/extension/ambarella_llm2_python"
SYSTEM_DIR="${SYSTEM_DIR:-$REPO_ROOT/ai_agents/agents/examples/voice-assistant/tenapp/ten_packages/system}"
SERVE="${SERVE:-$TOOLS/serve_qwen35.sh}"
CLEAR="${CLEAR:-$TOOLS/clear_meeting_worker.sh}"
UPLOADER="${MEETING_UPLOADER:-http://127.0.0.1:8765}"
LLM_PORT="${LLM_PORT:-8080}"
QWEN_PKG="${QWEN_PKG:-$(cat "$HOME/workspace/.qwen35-install/qwen_pkg" 2>/dev/null || echo "$HOME/demo_resources/llm_demo/qwen3.5_4B")}"
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
LLM_ADDON=$("$PY" - "$REPO_ROOT/ai_agents/agents/examples/voice-assistant/tenapp/property.json" "$GRAPH" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for g in data["ten"]["predefined_graphs"]:
    if g["name"] == sys.argv[2]:
        print(next((n["addon"] for n in g["graph"]["nodes"] if n["name"] == "llm"), ""))
EOF
)
[[ -n "$LLM_ADDON" ]] || { echo "no graph $GRAPH with an llm node"; exit 2; }
echo "graph   $GRAPH (llm: $LLM_ADDON)"

WAS=""
SWITCHED=0
SUDO_KEEP=""
# On any exit: the 7B given back if this run switched the board from it
# (unless --keep-qwen), sudo no longer kept fresh, the output through tee.
on_exit() {
  local rc=$?
  if ((SWITCHED)) && ((!KEEP_QWEN)) && [[ $WAS == 7b ]]; then "$SERVE" back 7b; fi
  [[ -n "$SUDO_KEEP" ]] && { pkill -P "$SUDO_KEEP" 2>/dev/null; kill "$SUDO_KEEP" 2>/dev/null; }
  exec >&- 2>&-
  wait "$TEE_PID" 2>/dev/null
  exit "$rc"
}
trap on_exit EXIT
if [[ "$LLM_ADDON" == openai_llm2_python ]]; then
  # Switching the LLM needs sudo minutes in; asked now, kept fresh.
  sudo -v || { echo "sudo is needed to switch the board's LLM"; exit 1; }
  ( while kill -0 "$$" 2>/dev/null; do sudo -n -v 2>/dev/null; sleep 50; done ) >/dev/null 2>&1 &
  SUDO_KEEP=$!
fi

# --- 1. the checkout carries the code under test -----------------------------
step "1. checkout"
git -C "$REPO_ROOT" log --oneline -1 || { failed "not a git checkout"; summary; exit 1; }
# Looked up in the whole history, without a pipe: a window of recent commits
# goes stale, and `git log | grep -q` dies of SIGPIPE under pipefail.
missing=0
for subject in \
  "count the orphans to mend a broken character" \
  "ask the conclusion for no owners at all" \
  "acknowledge segment_audio and transcribe at once" \
  "end a meeting failed whatever raises" \
  "check the record's script and a phone's meeting" \
  "send no system message when the prompt is empty" \
  "keep Qwen3.5's thinking off unless a request asks" \
  "meeting_minutes_qwen, the meeting graph on Qwen3.5-4B" \
  "check the graph's own LLM and vLLM's memory" \
  "run Qwen3.5-4B as a service, with tool calls" \
  "boot into Qwen3.5-4B, ten-api after it" \
  "the board's graphs on Qwen3.5-4B"
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

if [[ "$LLM_ADDON" == openai_llm2_python ]]; then
  # --- 2. Qwen3.5-4B: the template the graph relies on, and an answer --------
  step "2. Qwen3.5-4B: its chat template, and an answer"
  todo=$(python3 -I "$TOOLS/qwen35_prepare.py" model "$QWEN_PKG" --check 2>&1)
  echo "$todo"
  restart=0
  if grep -q "FAIL" <<<"$todo"; then
    failed "the Qwen3.5-4B package at $QWEN_PKG is not as expected; see above"
  elif grep -q "to do:" <<<"$todo"; then
    if python3 -I "$TOOLS/qwen35_prepare.py" model "$QWEN_PKG"; then
      passed "model package brought up to date (thinking off unless a request asks)"
      restart=1
    else
      failed "qwen35_prepare.py could not update $QWEN_PKG; see above"
    fi
  fi
  WAS=$("$SERVE" which)
  echo "  the board serves: $WAS"
  serving=0
  [[ $WAS == qwen ]] && serving=1
  # start and restart refuse while a meeting worker is up -- the phone app
  # leaves one -- and step 4 would clear it anyway: cleared now, when needed.
  if ((!serving || restart)) && [[ $(curl -s -m 2 -o /dev/null -w '%{http_code}' "$UPLOADER/meetings") != 000 ]]; then
    if "$CLEAR"; then
      passed "the meeting worker left up is cleared, so Qwen3.5-4B can switch"
    else
      failed "clear_meeting_worker.sh: a meeting worker is up and was not cleared; see above"
    fi
  fi
  if ((serving)); then
    if ((restart)); then
      "$SERVE" restart || failed "Qwen3.5-4B did not come back after the restart; see above"
    fi
  elif "$SERVE" start; then
    SWITCHED=1
  else
    failed "Qwen3.5-4B did not start; see above"
  fi
  if "$SERVE" test; then
    passed "Qwen3.5-4B answers, thinking off by default, no U+FFFD"
  else
    failed "Qwen3.5-4B's answer is not right; see above"
  fi

  # --- 3. openai_llm2_python's suite under the runtime's interpreter ---------
  step "3. openai_llm2_python tests"
  LLM_EXT="$REPO_ROOT/ai_agents/agents/ten_packages/extension/openai_llm2_python"
  if [[ ! -d "$SYSTEM_DIR/ten_runtime_python" ]]; then
    failed "no TEN runtime at $SYSTEM_DIR; run ai_agents/agents/scripts/install_board_arm64.sh"
  else
    out=$(TEN_SYSTEM_DIR="$SYSTEM_DIR" "$LLM_EXT/tests/bin/start" -q 2>&1)
    echo "$out" | tail -5
    count=""
    [[ "$out" =~ ([0-9]+)\ passed ]] && count="${BASH_REMATCH[1]}"
    if [[ -n "$count" ]] && ! [[ "$out" =~ [0-9]+\ (failed|error) ]]; then
      passed "openai_llm2_python: $count passed"
    else
      failed "openai_llm2_python tests did not pass; see above"
    fi
  fi
else
  # --- 2. the mender on every answer saved from the board --------------------
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

  # --- 3. the llm extension's suite under the runtime's interpreter ----------
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
fi

# --- 4. a whole meeting through the graph ------------------------------------
if [[ $MEETING -eq 1 ]]; then
  step "4. meeting through the graph"
  if "$CLEAR"; then
    passed "board ready: no meeting worker left, packages importable"
  else
    failed "clear_meeting_worker.sh: the board is not ready for a meeting; see above"
  fi
  args=(--audio "$AUDIO" --rttm "$RTTM" --speakers "$SPEAKERS")
  args+=(--graph "$GRAPH")
  [[ -n "$MINUTES" ]] && args+=(--minutes "$MINUTES")
  [[ -n "$SCRIPT" ]] && args+=(--script "$SCRIPT")
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
    # Headings in either script: the record follows the upload's choice.
    conclusion=$(awk '/^(結論|结论)$/ {on = 1; next} /^(話題|话题) / {exit} on' "$minutes_txt")
    owners=$(grep -cE '(负责人|負責人)[：: ]*[0-9]' <<<"$conclusion")
    titles=$(grep -cE '老师|老師|经理|經理|主任|园长|園長|校长|校長|先生|女士' <<<"$conclusion")
    broken=$(grep -c $'\xef\xbf\xbd' "$minutes_txt")
    if [[ $owners -eq 0 ]]; then
      passed "conclusion: no numbered owners (负责人N / 負責人N)"
    else
      failed "conclusion: $owners line(s) with numbered owners (负责人N / 負責人N)"
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

# --- 5. a meeting from the phone ---------------------------------------------
if [[ $PHONE -eq 1 ]]; then
  step "5. a meeting from the Android app"
  if "$CLEAR"; then
    "$PY" "$TOOLS/check_meeting_phone.py" --script "${SCRIPT:-traditional}"
    if [[ $? -eq 0 ]]; then
      passed "check_meeting_phone.py: all its checks passed"
    else
      failed "check_meeting_phone.py reported failures; see above"
    fi
  else
    failed "clear_meeting_worker.sh: the board is not ready for the phone; see above"
  fi
fi

# --- the board left on the LLM it served -------------------------------------
# Here rather than only on exit, so that the summary says what became of it.
if ((SWITCHED)) && [[ $WAS == 7b ]] && ((KEEP_QWEN)); then
  noted "--keep-qwen: Qwen3.5-4B left serving; serve_qwen35.sh stop gives the 7B back"
elif ((SWITCHED)) && [[ $WAS == 7b ]]; then
  step "the 7B given back"
  "$SERVE" back 7b || failed "serve_qwen35.sh back 7b did not finish; see above"
  # Not taken on its word: it says ok after a wait that ran out, too. The 7B
  # is back when its daemon runs and its port listens.
  daemon=no
  port=no
  pgrep -x test_llm >/dev/null && daemon=yes
  (exec 3<>"/dev/tcp/127.0.0.1/$LLM_PORT") 2>/dev/null && port=yes
  if [[ $daemon == yes && $port == yes ]]; then
    passed "the DeepSeek 7B is back: test_llm runs, $LLM_PORT listens"
  else
    failed "the DeepSeek 7B is not back (test_llm running: $daemon, $LLM_PORT listening: $port); tools/ambarella/serve_qwen35.sh status"
  fi
elif ((SWITCHED)); then
  noted "Qwen3.5-4B left serving: nothing served before this run, and every board graph asks it"
fi
SWITCHED=0

summary
for line in "${RESULTS[@]}"; do
  [[ "$line" == FAIL* ]] && exit 1
done
exit 0
