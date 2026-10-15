#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Compare the board's two LLMs on one meeting, end to end: the DeepSeek 7B
# daemon answers every prompt the meeting graph would send, then Qwen3.5-4B
# answers the same prompts, then the board is put back on the LLM it served
# before (Qwen3.5-4B, since the switch) and the two rounds are compared
# (compare_meeting_llm.py).
#
#   tools/ambarella/compare_meeting_llm.sh                 every topic of the newest
#                                                          Simplified board check
#   tools/ambarella/compare_meeting_llm.sh --topics 1-3    the first three topics only
#   tools/ambarella/compare_meeting_llm.sh --record PATH   another record.json
#   tools/ambarella/compare_meeting_llm.sh --resume        go on with the newest
#                                                          comparison, keeping its answers
#   tools/ambarella/compare_meeting_llm.sh --prompts explicit-owners
#                                                          both models asked a prompt set from
#                                                          ai_agents/agents/ten_packages/extension/
#                                                          meeting_control_python/prompt_variants.json
#
# With --prompts, each model's round is also compared with its round under
# the shipped prompts on the same record, from the newest comparison that
# has one: compare-<model>_shipped-vs-<model>_<set>.html beside compare.html.
#
# Run it on the board as the desktop user (lychee), not with sudo. It asks
# for the sudo password once, at the start, and keeps it fresh until it ends:
# the switches between the models (serve_qwen35.sh stop and start) come
# minutes in, and a prompt there timed out. The graphs have no Qwen3.5-4B
# while the 7B answers, so it refuses while a meeting worker is up. Whatever
# happens, the board is put back on the LLM it served before.
# Run it inside tmux or screen: a whole meeting takes a while. Everything is
# also written to <the comparison's folder>/compare.log.

set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT="$HOME/meeting_probe/llm_compare"
PY="${PY:-python3.12}"
QWEN_API="http://127.0.0.1:${QWEN_PORT:-8000}/v1"
QWEN_MODEL="qwen3.5_4B"
SERVE="${SERVE:-$HERE/serve_qwen35.sh}"
UPLOADER="${MEETING_UPLOADER:-http://127.0.0.1:8765}"
A="deepseek-7b"
B="qwen35-4b"

pass=()
RESUME=0
PROMPTS=shipped
while (($#)); do
  case "$1" in
    --topics|--record)
      [[ -n "${2:-}" ]] || { echo "$1 needs a value" >&2; exit 2; }
      pass+=("$1" "$2")
      shift
      ;;
    --prompts)
      [[ -n "${2:-}" ]] || { echo "$1 needs a value" >&2; exit 2; }
      PROMPTS="$2"
      shift
      ;;
    --resume) RESUME=1 ;;
    -h|--help) sed -n '6,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

ok() { echo "  ok    $*"; }
note() { echo "  note  $*"; }
fail() { echo "  FAIL  $*"; exit 1; }

if ((RESUME)); then
  OUT=$(ls -d "$ROOT"/*/ 2>/dev/null | sort | tail -1)
  OUT=${OUT%/}
  [[ -n "$OUT" ]] || { echo "nothing to resume under $ROOT" >&2; exit 1; }
else
  OUT="$ROOT/$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$OUT"
# tee ignores Ctrl-C and a hangup: they reach the whole foreground group, and
# a tee gone with them would take on_exit's give-back down with SIGPIPE.
exec > >(trap '' INT HUP; exec tee -a "$OUT/compare.log") 2>&1
TEE_PID=$!
WAS=""
SWITCHED=0
SUDO_KEEP=""
# On any exit: the board put back on the LLM it served before, if this
# switched it; sudo no longer kept fresh; then all the output through tee
# before the prompt comes back.
on_exit() {
  local rc=$?
  ((SWITCHED)) && "$SERVE" back "$WAS"
  # The loop and the sleep it is in.
  [[ -n "$SUDO_KEEP" ]] && { pkill -P "$SUDO_KEEP" 2>/dev/null; kill "$SUDO_KEEP" 2>/dev/null; }
  exec >&- 2>&-
  wait "$TEE_PID" 2>/dev/null
  exit "$rc"
}
trap on_exit EXIT

echo "== $(date '+%F %T') compare_meeting_llm.sh ${pass[*]} --prompts $PROMPTS (in $OUT)"
echo "== preflight"
[[ $(id -u) -ne 0 ]] || fail "run it as the desktop user (lychee), not with sudo"
# Asked now, while someone is at the terminal; kept fresh in the background.
sudo -v || fail "sudo is needed to switch the board's LLM (stop and start ambarella-llm)"
( while kill -0 "$$" 2>/dev/null; do sudo -n -v 2>/dev/null; sleep 50; done ) >/dev/null 2>&1 &
SUDO_KEEP=$!
ok "sudo, kept fresh until this ends"
command -v "$PY" >/dev/null || fail "no $PY: the prompts come from meeting_control_python, which runs under it"
"$PY" -c 'import pydantic' 2>/dev/null || fail "$PY cannot import pydantic (meeting_control_python's config.py needs it)"
ok "$PY with pydantic"
if [[ "$PROMPTS" != shipped ]]; then
  "$PY" -c 'import json, sys; sys.exit(sys.argv[2] not in json.load(open(sys.argv[1])))' \
    "$HERE/../../ai_agents/agents/ten_packages/extension/meeting_control_python/prompt_variants.json" "$PROMPTS" ||
    fail "no prompt set $PROMPTS in ai_agents/agents/ten_packages/extension/meeting_control_python/prompt_variants.json"
fi
ok "prompts: $PROMPTS"
[[ -x "$HOME/workspace/cooper-vllm/venv_vllm/bin/vllm" ]] ||
  fail "Qwen3.5-4B is not installed: tools/ambarella/install_qwen35_vllm.sh"
ok "Qwen3.5-4B is installed"
if [[ $(curl -s -m 2 -o /dev/null -w '%{http_code}' "$UPLOADER/meetings") != 000 ]]; then
  fail "a meeting worker is up ($UPLOADER): the graphs would lose their LLM. Let the board reap it, or run tools/ambarella/clear_meeting_worker.sh"
fi
ok "no meeting worker is up"
WAS=$("$SERVE" which)
ok "the board serves: $WAS"
if [[ $WAS != 7b ]]; then
  note "switching to the 7B, so that it answers first"
  SWITCHED=1
  "$SERVE" stop || fail "could not switch to the 7B"
fi
pgrep -x test_llm >/dev/null ||
  fail "the DeepSeek 7B daemon is not running: tools/ambarella/serve_qwen35.sh stop"
ok "the DeepSeek 7B daemon is running"

echo "== round 1: DeepSeek 7B"
"$PY" "$HERE/compare_meeting_llm.py" run --label "$A" --out "$OUT" --prompts "$PROMPTS" "${pass[@]}" ||
  fail "round 1 stopped (above); run this again with --resume to go on"

echo "== switching to Qwen3.5-4B"
SWITCHED=1
"$SERVE" start || fail "Qwen3.5-4B did not start (above)"

echo "== round 2: Qwen3.5-4B"
"$PY" "$HERE/compare_meeting_llm.py" run --label "$B" --out "$OUT" \
  --openai "$QWEN_API" --model "$QWEN_MODEL" --prompts "$PROMPTS" "${pass[@]}" ||
  fail "round 2 stopped (above); run this again with --resume to go on"

echo "== putting the board back: $WAS"
"$SERVE" back "$WAS"
SWITCHED=0

echo "== comparison"
"$PY" "$HERE/compare_meeting_llm.py" compare "$OUT" "$A" "$B" || fail "the comparison failed (above)"

if [[ "$PROMPTS" != shipped ]]; then
  echo "== each model against its own round under the shipped prompts"
  # The newest other comparison whose rounds were asked the shipped prompts
  # on the same record.
  BASE=$("$PY" - "$ROOT" "$OUT" "$A" "$B" <<'EOF'
import glob, json, os, sys
root, out, labels = sys.argv[1], sys.argv[2], sys.argv[3:]
def load(folder, label):
    try:
        with open(os.path.join(folder, label, "run.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
record = load(out, labels[0]) or {}
for folder in sorted(glob.glob(os.path.join(root, "*")), reverse=True):
    if os.path.realpath(folder) == os.path.realpath(out):
        continue
    rounds = [load(folder, label) for label in labels]
    if all(r and r.get("prompts", "shipped") == "shipped"
           and r.get("record_sha") == record.get("record_sha")
           and r.get("ended") for r in rounds):
        print(folder)
        break
EOF
)
  if [[ -z "$BASE" ]]; then
    note "no earlier comparison under the shipped prompts on this record: run one without --prompts"
  else
    ok "against $BASE"
    for label in "$A" "$B"; do
      "$PY" "$HERE/compare_meeting_llm.py" compare "$OUT" "$BASE/$label" "$label" ||
        note "could not compare $label with $BASE/$label"
    done
  fi
fi
echo
echo "  folder: $OUT"
echo "  to read it on the dev machine:"
echo "    scp lychee@$(hostname -I 2>/dev/null | awk '{print $1}'):${OUT#$HOME/}/compare.html ."
