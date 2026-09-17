#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Turn the model's reasoning phase off, or back on, the way Ambarella says to.
#
#   tools/ambarella/set_llm_thinking.sh            # report, change nothing
#   tools/ambarella/set_llm_thinking.sh --off      # bypass the think phase
#   tools/ambarella/set_llm_thinking.sh --on       # put it back
#   tools/ambarella/set_llm_thinking.sh --model-dir /path/to/deepseek_7B
#
# The model's prompt template lives in prompt_config.json beside the weights.
# Ending the assistant marker with an already-closed think block means the
# model starts generating after it, so the answer comes first instead of
# 44-523 characters of reasoning. Measured on this board, that reasoning was
# the whole of a 17-36 second wait -- see docs/development/board_llm_measurements.md.
#
#   "Symbol1": "<｜Assistant｜>"                    thinking on  (or absent)
#   "Symbol1": "<｜Assistant｜><think>\n\n</think>"  thinking bypassed
#
# The daemon reads this at load, so it has to be restarted afterwards. This
# script does not restart it: the launcher takes 80 seconds to come back and
# stopping it is the operator's call. It prints what to run.
#
# Backs up before writing, and never edits a file it did not first parse.

set -uo pipefail

MODE="check"
MODEL_DIR="${AMBARELLA_LLM_MODEL_DIR:-}"
GUIDE_MODEL_DIR="$HOME/demo_resources/llm_demo/deepseek_7B"
LAUNCHER="/usr/share/ambarella/llm_demo/run_llm_demo.sh"

# The exact strings from Ambarella. The vertical bars are U+FF5C and the
# underscore in begin_of_sentence is U+2581; they are not ASCII and must not
# be "tidied".
ASSISTANT='<｜Assistant｜>'
NO_THINK='<think>\n\n</think>'

while [[ $# -gt 0 ]]; do
  case "$1" in
    --off)       MODE="off"; shift ;;
    --on)        MODE="on"; shift ;;
    --check)     MODE="check"; shift ;;
    --model-dir) MODEL_DIR="$2"; shift 2 ;;
    -h|--help)   sed -n '6,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

die()  { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }
sec()  { printf '\n\033[1m===== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
info() { printf '        %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }

# ------------------------------------------------------------------ 1
sec "1. The model directory"

# Prefer what the running client was actually given over what the guide says:
# a board can have several model directories and only one of them is live.
if [[ -z "$MODEL_DIR" ]]; then
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    argv=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
    case "$argv" in
      test_llm_client*)
        from_argv=$(echo "$argv" | sed -n 's/.*-m \([^ ]*\).*/\1/p')
        [[ -n "$from_argv" ]] && MODEL_DIR="$from_argv" && break
        ;;
    esac
  done < <(pgrep -x 'test_llm|test_llm_client' 2>/dev/null)
  [[ -n "$MODEL_DIR" ]] && ok "from the running client: $MODEL_DIR"
fi
if [[ -z "$MODEL_DIR" ]]; then
  MODEL_DIR="$GUIDE_MODEL_DIR"
  info "nothing running; using the guide's path"
fi
[[ -d "$MODEL_DIR" ]] || die "no model directory at $MODEL_DIR
       Pass --model-dir, or start the LLM so it can be read from the client."
ok "$MODEL_DIR"

CONFIG="$MODEL_DIR/prompt_config.json"
if [[ -e "$CONFIG" ]]; then
  REAL=$(readlink -f "$CONFIG")
  if [[ "$REAL" != "$CONFIG" ]]; then
    # deepseek_7B is a directory of symlinks into demo_resources/models, so
    # the file being edited may be shared with another model directory.
    warn "prompt_config.json is a symlink to"
    warn "  $REAL"
    warn "anything else pointing there changes too"
  fi
  ok "prompt_config.json present"
else
  info "no prompt_config.json yet; --off would create one"
fi

# ------------------------------------------------------------------ 2
sec "2. What it says now"

STATE=$(CONFIG="$CONFIG" ASSISTANT="$ASSISTANT" NO_THINK="$NO_THINK" python3 - <<'PY'
import json, os, sys

path = os.environ["CONFIG"]
if not os.path.exists(path):
    print("MISSING")
    sys.exit(0)
try:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
except Exception as err:                      # noqa: BLE001
    print(f"UNPARSEABLE {err}")
    sys.exit(0)

sym1 = (
    doc.get("PromptRender", {}).get("Elements", {}).get("Symbol1")
)
if sym1 is None:
    print("NO_SYMBOL1")
elif sym1.endswith("</think>"):
    print("OFF")          # thinking bypassed
else:
    print("ON")           # thinking active
print(json.dumps({"Symbol1": sym1}, ensure_ascii=False))
PY
)
VERDICT=$(echo "$STATE" | head -1)
DETAIL=$(echo "$STATE" | tail -n +2)

case "$VERDICT" in
  MISSING)      info "the file does not exist" ;;
  UNPARSEABLE*) die "prompt_config.json is not valid JSON: ${VERDICT#UNPARSEABLE }
       Nothing was changed. Fix or remove it first." ;;
  NO_SYMBOL1)   info "no PromptRender.Elements.Symbol1 in it" ;;
  ON)           ok "thinking is ON   $DETAIL" ;;
  OFF)          ok "thinking is BYPASSED   $DETAIL" ;;
esac

if [[ "$MODE" == "check" ]]; then
  echo
  echo "  --off to bypass the think phase, --on to restore."
  exit 0
fi

# ------------------------------------------------------------------ 3
sec "3. Writing"

if [[ "$MODE" == "on" && "$VERDICT" != "OFF" ]]; then
  ok "already on; nothing to do"
  exit 0
fi
if [[ "$MODE" == "off" && "$VERDICT" == "OFF" ]]; then
  ok "already bypassed; nothing to do"
  exit 0
fi

if [[ -e "$CONFIG" ]]; then
  BACKUP="$CONFIG.bak.$(date +%Y%m%d_%H%M%S)"
  cp -L "$CONFIG" "$BACKUP" || die "could not back up to $BACKUP"
  ok "backup: $BACKUP"
fi

MODE="$MODE" CONFIG="$CONFIG" ASSISTANT="$ASSISTANT" NO_THINK="$NO_THINK" python3 - <<'PY' || die "the file was not written"
import json, os

path = os.environ["CONFIG"]
mode = os.environ["MODE"]
assistant = os.environ["ASSISTANT"]
no_think = os.environ["NO_THINK"].encode().decode("unicode_escape")

# Ambarella's own structure, used only when there is no file to start from.
TEMPLATE = {
    "Version": "1.0.0",
    "PromptRender": {
        "Template": {
            "FreshStart": ["Symbol0", "UserPrompt", "Symbol1"],
            "FollowUp": ["Symbol2", "UserPrompt", "Symbol1"],
            "SoftReset": [
                "Symbol0", "LastPrompt", "Symbol3", "LastResponse",
                "Symbol4", "Symbol2", "UserPrompt", "Symbol1",
            ],
        },
        "Elements": {
            "Symbol0": "<｜begin▁of▁sentence｜><｜User｜>",
            "Symbol1": assistant,
            "Symbol2": "<｜User｜>",
            "Symbol3": "<｜Assistant｜>",
            "Symbol4": "<｜end▁of▁sentence｜>",
        },
    },
}

if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
else:
    doc = TEMPLATE

elements = doc.setdefault("PromptRender", {}).setdefault("Elements", {})
current = elements.get("Symbol1", assistant)

if mode == "off":
    base = current[: -len("<think>\n\n</think>")] if current.endswith("</think>") else current
    elements["Symbol1"] = base + no_think
else:
    # Strip exactly what was appended, leaving whatever else was there.
    marker = current.rfind("<think>")
    elements["Symbol1"] = current[:marker] if marker >= 0 else current

# Written through the symlink on purpose: that is the file the daemon reads.
with open(path, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, ensure_ascii=False, indent=2)
    fh.write("\n")

print(f"        Symbol1 is now {json.dumps(elements['Symbol1'], ensure_ascii=False)}")
PY

ok "written"

# ------------------------------------------------------------------ 4
sec "4. Restart, then verify"

cat <<NEXT

  The daemon reads prompt_config.json when it loads the model, so nothing
  changes until it is restarted. It takes about 80 seconds to come back.

NEXT

if [[ -x "$LAUNCHER" ]]; then
  modes=$(grep -oE '\-\-run_mode[[:space:]=]+[a-z]+|"(start|stop|restart)"' \
    "$LAUNCHER" 2>/dev/null | grep -oE '(start|stop|restart)' | sort -u | tr '\n' ' ')
  info "run_modes this launcher mentions: ${modes:-unknown}"
  echo
  info "cd $(dirname "$LAUNCHER")"
  info "./$(basename "$LAUNCHER") --run_mode start --model_type 9 \\"
  info "    --model_path $(dirname "$MODEL_DIR") --ip 127.0.0.1 --max_user 1"
else
  warn "no launcher at $LAUNCHER"
fi

cat <<NEXT

  Wait for a 'Device ENABLE' line in /tmp/log.txt, then:

    tools/ambarella/check_llm_board.sh
    python3 tools/ambarella/probe_llm_thinking.py --only "as deployed" --repeat 2

  In that output, 'think 0.0s' with 'reason 0' means the reply carried no
  </think> at all, which is the bypass working. Check 'reply' is still a
  normal length -- a short reply with no thinking is the model refusing, not
  answering faster, and the probe prints the first words so the two can be
  told apart.

  To undo:  tools/ambarella/set_llm_thinking.sh --on

NEXT
