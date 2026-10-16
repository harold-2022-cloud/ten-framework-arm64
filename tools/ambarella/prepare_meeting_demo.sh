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
#   2 desktop    a graphical session of this user, the kiosk's browser in it,
#                held by kde-inhibit on KDE so the display stays awake;
#                whether the board logs in by itself at boot (a note)
#   3 font       a Traditional Chinese font (fc-list :lang=zh-tw)
#   4 meeting    check_meeting_board.py --check under python3.12: models,
#                packages, where meetings land, the graph, the LLM; before
#                --warm, since it wants 8765 free
#   5 showcase   MEETING_SCREEN_SHOWCASE names a meeting, it is archived, and
#                the running screen shows it (the service reads .env only
#                when install_board_services.sh installs it)
#   6 warm       with --warm only: /start the meeting worker on channel
#                meeting-room for 1800 s, and wait until 8765 answers; as it
#                starts, the worker marks failed any meeting left mid-way
#   7 leftovers  no meeting left mid-way with no worker to finish it, and
#                none queued from before: a queued one is processed before
#                a new upload
#
# Run it on the board as the desktop user (lychee), not with sudo. Exits
# non-zero if anything is not ready. Its log: /tmp/prepare_meeting_demo_*.log
# (LOG_DIR), brought back by pull_board_check.sh.
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

# Everything it says also goes to a log of the day, which
# tools/ambarella/pull_board_check.sh brings back. tee ignores Ctrl-C and a
# hangup, as in verify_meeting_board.sh, so the log keeps the last lines.
LOG="${LOG_DIR:-/tmp}/prepare_meeting_demo_$(date +%Y%m%d_%H%M%S).log"
exec > >(trap '' INT HUP; exec tee "$LOG") 2>&1
TEE_PID=$!
on_exit() {
  local rc=$?
  echo "  log: $LOG"
  exec >&- 2>&-
  wait "$TEE_PID" 2>/dev/null
  exit "$rc"
}
trap on_exit EXIT

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
# On KDE (Wayland, where xset reaches nothing) only kde-inhibit around the
# browser keeps the display from dimming, turning off and locking.
if pgrep -u "$ME" -x plasmashell >/dev/null; then
  if pgrep -u "$ME" -f -- "kde-inhibit.*$PROFILE" >/dev/null; then
    ok "kde-inhibit keeps the display awake while the kiosk's browser runs"
  else
    bad "the kiosk's browser is not held by kde-inhibit: the display may dim, turn off or lock (see /tmp/meeting_screen_kiosk.log)"
  fi
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
  elif answers "$SCREEN_URL/api/view"; then
    served=$(curl -s -m 3 "$SCREEN_URL/api/view" | python3 -I -c 'import json, sys
try:
    print(json.load(sys.stdin).get("showcase_id") or "")
except (ValueError, AttributeError):
    print("")')
    if [[ "$served" == "$SHOWCASE" ]]; then
      ok "showcase $SHOWCASE is there and archived, and the screen shows it"
    else
      bad "showcase $SHOWCASE is set, but the screen shows ${served:-none}: tools/ambarella/install_board_services.sh again, so its service reads ai_agents/.env"
    fi
  else
    ok "showcase $SHOWCASE is there and archived (the screen does not answer: see 1)"
  fi
fi

if ((WARM)); then
  echo "== 6 warm"
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

echo "== 7 leftovers"
# After a warm-up: the worker it started has marked failed what was left
# mid-way (store.fail_interrupted), and takes what is queued in turn.
leftovers=$(python3 -I - "$MEETINGS_DIR" <<'EOF'
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
        print(f"going {name} ({state})")
    elif state == "queued":
        print(f"queued {name}")
EOF
)
going=$(sed -n 's/^going //p' <<<"$leftovers" | tr '\n' ' ')
queued=$(sed -n 's/^queued //p' <<<"$leftovers" | tr '\n' ' ')
up=0
answers "$UPLOADER/meetings" && up=1
if [[ -z "$going" ]]; then
  ok "no meeting left mid-way"
elif ((up)); then
  note "being processed now: $going"
else
  bad "meetings left mid-way with no worker to finish them: ${going% } -- tools/ambarella/prepare_meeting_demo.sh --warm: the worker it starts marks them failed"
fi
if [[ -z "$queued" ]]; then
  ok "no meeting queued from before"
elif ((up)); then
  bad "queued from before, each processed before a new upload: ${queued% } -- the worker is on them; run this again once they are done"
else
  bad "queued from before, each processed before a new upload: ${queued% } -- tools/ambarella/prepare_meeting_demo.sh --warm well before the demo, and let them finish"
fi

echo
if ((${#FAILS[@]})); then
  echo "not ready: ${#FAILS[@]} thing(s)"
  for f in "${FAILS[@]}"; do echo "  - $f"; done
  exit 1
fi
echo "ready for the demo"
