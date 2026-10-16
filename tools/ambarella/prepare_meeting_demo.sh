#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Before a demo: check in one go what the meeting demo screen and the
# meeting minutes rely on, and say what is not ready.
#
#   tools/ambarella/prepare_meeting_demo.sh          check
#   tools/ambarella/prepare_meeting_demo.sh --warm   and start the meeting worker,
#                                                    so the first upload need not wait
#
#   1 services   qwen35-vllm, ten-api and meeting-screen running; the screen's
#                /api/view answering
#   2 desktop    a graphical session of this user, the kiosk's browser in it;
#                whether the board logs in by itself at boot (a note)
#   3 font       a Traditional Chinese font (fc-list :lang=zh-tw)
#   4 meeting    check_meeting_board.py --check under python3.12: models,
#                packages, where meetings land, the graph, the LLM; before
#                --warm, since it wants 8765 free
#   5 showcase   MEETING_SCREEN_SHOWCASE names a meeting, and it is archived
#   6 stuck      no meeting left between received and concluding with no
#                worker to finish it (clear_meeting_worker.sh clears them)
#   7 warm       with --warm: /start the meeting worker on channel
#                meeting-room for 1800 s, and wait until 8765 answers
#
# Run it on the board as the desktop user (lychee), not with sudo. Exits
# non-zero if anything is not ready.
#
# Settings, from the environment or ai_agents/.env: MEETINGS_DIR,
# MEETING_SCREEN_SHOWCASE, SERVER_PORT; from the environment: SCREEN_URL
# (http://127.0.0.1:8766), MEETING_UPLOADER, CHECK_PY (python3.12),
# KIOSK_PROFILE (~/.cache/meeting-screen-browser).

set -uo pipefail

WARM=0
case "${1:-}" in
  "") ;;
  --warm) WARM=1 ;;
  -h|--help) sed -n '6,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$HERE/../.." && pwd)
ENV_FILE="${ENV_FILE:-$REPO_ROOT/ai_agents/.env}"
from_env_file() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'"; }
MEETINGS_DIR="${MEETINGS_DIR:-$(from_env_file MEETINGS_DIR)}"
MEETINGS_DIR="${MEETINGS_DIR:-/tmp/meetings}"
SHOWCASE="${MEETING_SCREEN_SHOWCASE:-$(from_env_file MEETING_SCREEN_SHOWCASE)}"
port=$(from_env_file SERVER_PORT)
SERVER="http://127.0.0.1:${port:-8081}"
SCREEN_URL="${SCREEN_URL:-http://127.0.0.1:8766}"
UPLOADER="${MEETING_UPLOADER:-http://127.0.0.1:8765}"
CHECK_PY="${CHECK_PY:-python3.12}"
PROFILE="${KIOSK_PROFILE:-$HOME/.cache/meeting-screen-browser}"
ME=$(id -un)

FAILS=()
ok() { echo "  ok    $*"; }
note() { echo "  note  $*"; }
bad() { FAILS+=("$*"); echo "  FAIL  $*"; }
answers() { curl -s -m 3 -o /dev/null "$1"; }

echo "== 1 services"
for unit in qwen35-vllm ten-api meeting-screen; do
  if systemctl is-active --quiet "$unit"; then
    ok "$unit running"
  else
    bad "$unit is not running: tools/ambarella/install_board_services.sh"
  fi
done
if answers "$SCREEN_URL/api/view"; then
  ok "the screen answers at $SCREEN_URL"
else
  bad "nothing answers at $SCREEN_URL/api/view"
fi

echo "== 2 desktop"
types=""
while read -r session _ user _; do
  [[ "$user" == "$ME" ]] && types+="$(loginctl show-session "$session" -p Type --value 2>/dev/null) "
done < <(loginctl list-sessions --no-legend 2>/dev/null)
if [[ " $types " == *" wayland "* || " $types " == *" x11 "* ]]; then
  ok "$ME has a desktop session"
else
  bad "no desktop session of $ME: log in on the board's screen"
fi
if pgrep -u "$ME" -f -- "$PROFILE" >/dev/null; then
  ok "the kiosk's browser is up"
else
  bad "the kiosk's browser is not running: tools/ambarella/install_board_services.sh --kiosk on, then log in again"
fi
if "$HERE/set_desktop_autologin.sh" --status 2>/dev/null | grep -q "autologin: $ME into"; then
  ok "the board logs $ME in at boot"
else
  note "the board does not log in at boot: after a power cut someone logs in on it for the screen to return (tools/ambarella/set_desktop_autologin.sh)"
fi

echo "== 3 font"
font=$(fc-list :lang=zh-tw family 2>/dev/null | head -1)
if [[ -n "$font" ]]; then
  ok "a Traditional Chinese font: $font"
else
  bad "no Traditional Chinese font: the screen's Chinese would show as boxes"
fi

echo "== 4 meeting"
if answers "$UPLOADER/meetings"; then
  note "a meeting worker is up: check_meeting_board.py --check wants 8765 free; skipped"
else
  out=$("$CHECK_PY" "$HERE/check_meeting_board.py" --check 2>&1)
  if [[ $? -eq 0 ]]; then
    ok "check_meeting_board.py --check: all its checks passed"
  else
    grep -E "FAIL|->" <<<"$out" | sed 's/^/    /'
    bad "check_meeting_board.py --check failed (above)"
  fi
fi

echo "== 5 showcase"
state_of() {
  python3 -I -c 'import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("state") or "")
except (OSError, ValueError, AttributeError):
    print("")' "$MEETINGS_DIR/$1/work/state.json"
}
if [[ -z "$SHOWCASE" ]]; then
  bad "no MEETING_SCREEN_SHOWCASE in ai_agents/.env: PageDown has nothing to show"
else
  shown=$(state_of "$SHOWCASE")
  if [[ -z "$shown" ]]; then
    bad "showcase $SHOWCASE: no such meeting in $MEETINGS_DIR"
  elif [[ "$shown" != archived ]]; then
    bad "showcase $SHOWCASE is $shown, not archived"
  else
    ok "showcase $SHOWCASE is there and archived"
  fi
fi

echo "== 6 stuck"
stuck=$(python3 -I - "$MEETINGS_DIR" <<'EOF'
import json, os, sys
going = ("received", "decoding", "transcribing", "linking", "summarising", "concluding")
root = sys.argv[1]
try:
    names = sorted(os.listdir(root))
except OSError:
    names = []
for name in names:
    try:
        with open(os.path.join(root, name, "work", "state.json"), encoding="utf-8") as f:
            state = json.load(f).get("state")
    except (OSError, ValueError, AttributeError):
        continue
    if state in going:
        print(f"{name} ({state})")
EOF
)
if [[ -z "$stuck" ]]; then
  ok "no meeting left mid-way"
elif answers "$UPLOADER/meetings"; then
  note "being processed now: $(echo $stuck)"
else
  bad "meetings left mid-way with no worker to finish them: $(echo $stuck) -- tools/ambarella/clear_meeting_worker.sh"
fi

if ((WARM)); then
  echo "== 7 warm"
  if answers "$UPLOADER/meetings"; then
    ok "the meeting worker is up already"
  else
    reply=$(curl -s -m 10 -H 'Content-Type: application/json' \
      -d "{\"request_id\": \"prepare-$$\", \"channel_name\": \"meeting-room\", \"graph_name\": \"meeting_minutes\", \"timeout\": 1800}" \
      "$SERVER/start")
    for _ in $(seq 60); do
      answers "$UPLOADER/meetings" && break
      sleep 1
    done
    if answers "$UPLOADER/meetings"; then
      ok "the meeting worker is up: the first upload need not wait for it"
    else
      bad "/start answered '$reply', but $UPLOADER did not answer within 60 s"
    fi
  fi
fi

echo
if ((${#FAILS[@]})); then
  echo "not ready: ${#FAILS[@]} thing(s)"
  for f in "${FAILS[@]}"; do echo "  - $f"; done
  exit 1
fi
echo "ready for the demo"
