#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Make the board serve from power-on with nothing to type afterwards: its
# systemd services, enabled and started. Run once, and again after a pull.
#
#   tools/ambarella/install_board_services.sh              install, enable, start
#   git pull --ff-only && tools/ambarella/install_board_services.sh   update
#   tools/ambarella/install_board_services.sh --status     is each one up
#   tools/ambarella/install_board_services.sh --print      the unit files; nothing changes
#   tools/ambarella/install_board_services.sh --uninstall  stop, disable, remove
#   tools/ambarella/install_board_services.sh --kiosk on   the demo screen full screen
#                                                          whenever the desktop starts
#   tools/ambarella/install_board_services.sh --kiosk off  not any more
#
#   qwen35-vllm    Qwen3.5-4B on vLLM, port 8000, the LLM every board graph
#                  asks: serve_qwen35.sh exec, as you, its output appended to
#                  ~/workspace/vllm_qwen35.log by the process itself; started
#                  when /v1/models answers (serve_qwen35.sh ready, up to 300 s;
#                  loading takes about 55); restarted if it dies. Starting it
#                  stops ambarella-llm and the other way round: one model on
#                  the NPU at a time
#   ambarella-llm  the vendor's DeepSeek 7B daemon on 8080, installed but not
#                  started at boot: serve_qwen35.sh stop switches to it, for
#                  the LLM comparison or as a way back. run_llm_demo.sh
#                  --run_mode start at its start and stop at its stop, run
#                  from its own directory so its log stays /tmp/log.txt; as
#                  root, which is what its processes run as anyway
#   ten-api        the Go API server on SERVER_PORT (8081): `task
#                  run-api-server` in examples/voice-assistant, as you, with
#                  this terminal's environment (written to /etc/ten-api.env,
#                  readable by root only) -- the same .env and environment as
#                  a run by hand; started after qwen35-vllm, so that it takes
#                  no request before the LLM answers; restarted if it dies;
#                  its output appended to /tmp/task_run.log, where the checks
#                  look
#   meeting-screen the meeting demo screen's service on 127.0.0.1:8766: python3
#                  -m meeting_screen in ai_agents/meeting-screen, as you, on
#                  the board's own python3 (standard library only); it reads
#                  the meetings in MEETINGS_DIR and serves the page the kiosk
#                  shows; MEETING_SCREEN_SHOWCASE names the meeting PageDown
#                  shows (both from the environment or ai_agents/.env);
#                  restarted if it dies; its output appended to
#                  /tmp/meeting_screen.log by the process itself
#
# With these up, any phone can ask for meeting minutes at any time: the
# server starts the meeting worker for the request, the record stays on the
# board's disk, and the board reaps the idle worker.
#
# A server started by hand (task run) holds the port, so it is stopped and the
# service takes over -- the playground stops with it; `task run-frontend` in
# examples/voice-assistant brings it back. The board is switched to
# Qwen3.5-4B under its service -- the 7B stopped if it serves, a Qwen3.5-4B
# started by hand replaced -- but not while a meeting worker is up, whose
# meeting would lose the LLM halfway; the services then take over at the
# next boot, or when this runs again.
#
# Run it again after every git pull: when the API server's Go code changed,
# server/bin/api is built again and ten-api restarted, and when
# serve_qwen35.sh starts vLLM on other arguments than the running one's,
# qwen35-vllm is restarted -- neither while a meeting worker is up, so a
# meeting being processed is not cut short; it then says to run this again
# later.
#
# The kiosk starts with the desktop (~/.config/autostart, no sudo): the board
# must be logged in for it -- by hand, or at every boot through
# tools/ambarella/set_desktop_autologin.sh.
#
# Run it as the user who runs `task run`, not with sudo: sudo asks for your
# password for systemctl and /etc/systemd/system. The LLM daemon's options
# follow board_quickstart.md and can be changed through the environment:
#   LLM_MODEL_TYPE (9), LLM_MODEL_PATH (~/demo_resources/llm_demo), LLM_MAX_USER (1)

set -uo pipefail

MODE=install
KIOSK=""
case "${1:-}" in
  "") ;;
  --status) MODE=status ;;
  --print) MODE=print ;;
  --uninstall) MODE=uninstall ;;
  --kiosk)
    MODE=kiosk
    KIOSK="${2:-}"
    [[ $KIOSK == on || $KIOSK == off ]] || { echo "--kiosk takes on or off" >&2; exit 2; }
    ;;
  -h|--help) sed -n '6,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac

if [[ $MODE != print ]]; then
  # Everything but --print also goes to a log of the day, which
  # tools/ambarella/pull_board_check.sh brings back. tee ignores Ctrl-C and a
  # hangup, as in verify_meeting_board.sh, so the log keeps the last lines.
  LOG="${LOG_DIR:-/tmp}/install_board_services_$(date +%Y%m%d_%H%M%S).log"
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
fi

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
EXAMPLE="$REPO_ROOT/ai_agents/agents/examples/voice-assistant"
SERVER_DIR="${SERVER_DIR:-$REPO_ROOT/ai_agents/server}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/ai_agents/.env}"
SERVE="${SERVE:-$REPO_ROOT/tools/ambarella/serve_qwen35.sh}"
SCREEN_DIR="${SCREEN_DIR:-$REPO_ROOT/ai_agents/meeting-screen}"
SCREEN_PY="${SCREEN_PY:-/usr/bin/python3}"
SCREEN_PORT="${SCREEN_PORT:-8766}"
SCREEN_LOG="${SCREEN_LOG:-/tmp/meeting_screen.log}"
KIOSK_SCRIPT="${KIOSK_SCRIPT:-$REPO_ROOT/tools/ambarella/meeting_screen_kiosk.sh}"
AUTOSTART="${AUTOSTART:-$HOME/.config/autostart/meeting-screen.desktop}"
QWEN_VLLM="${QWEN_VLLM:-$HOME/workspace/cooper-vllm/venv_vllm/bin/vllm}"
UPLOADER="${MEETING_UPLOADER:-http://127.0.0.1:8765}"
TASK_LOG="${TASK_LOG:-/tmp/task_run.log}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
ENV_OUT="${ENV_OUT:-/etc/ten-api.env}"
LLM_DIR="${LLM_DIR:-/usr/share/ambarella/llm_demo}"
LLM_MODEL_TYPE="${LLM_MODEL_TYPE:-9}"
LLM_MODEL_PATH="${LLM_MODEL_PATH:-$HOME/demo_resources/llm_demo}"
LLM_MAX_USER="${LLM_MAX_USER:-1}"
TASK="${TASK:-$(command -v task || true)}"
RUN_USER=$(id -un)

port=$(grep -E '^SERVER_PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"'"'"' ')
SERVER="http://127.0.0.1:${port:-8081}"
# What ai_agents/.env says, quotes off; the environment wins over it.
from_env_file() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'"; }
# Where meetings land, as the graph's uploader and the checks read it.
MEETINGS_DIR="${MEETINGS_DIR:-$(from_env_file MEETINGS_DIR)}"
MEETINGS_DIR="${MEETINGS_DIR:-/tmp/meetings}"
SHOWCASE="${MEETING_SCREEN_SHOWCASE:-$(from_env_file MEETING_SCREEN_SHOWCASE)}"
SCREEN="http://127.0.0.1:$SCREEN_PORT"

ok() { echo "  ok    $*"; }
note() { echo "  note  $*"; }
fail() { echo "  FAIL  $*"; exit 1; }

# Processes whose command line matches, never this script or its parent.
pids_of() {
  pgrep -f "$1" | grep -vx -e "$$" -e "$PPID" || true
}

answers() {
  curl -s -m 3 -o /dev/null "$1"
}

# One value as a unit file wants it: quoted, so a space does not split it,
# with % doubled (systemd's specifiers) and " and \ escaped.
q() {
  local v=${1//\\/\\\\}
  v=${v//\"/\\\"}
  v=${v//%/%%}
  printf '"%s"' "$v"
}

# This terminal's environment as an EnvironmentFile, but for what belongs to
# the login session. A worker started by the service with PATH and HOME
# alone died in the tenapp's start script (exit code None), while the same
# start by hand, with everything the shell had sourced, ran.
environment() {
  env -0 | while IFS= read -r -d '' kv; do
    key=${kv%%=*}
    value=${kv#*=}
    case "$key" in
      PWD|OLDPWD|SHLVL|_|TERM|COLORTERM|LS_COLORS|PS1|PS2|PS4|PROMPT_COMMAND|\
      MAIL|MOTD_SHOWN|DISPLAY|WAYLAND_DISPLAY|DBUS_SESSION_BUS_ADDRESS|\
      XDG_RUNTIME_DIR|XDG_SESSION_*|XDG_SEAT|XDG_VTNR|SSH_*|SUDO_*|HIST*|\
      BASH_FUNC_*|TMUX*|STY|WINDOW|LESSOPEN|LESSCLOSE|ENV_OUT|UNIT_DIR) continue ;;
    esac
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ "$value" == *$'\n'* ]] && continue
    # Inside double quotes systemd takes \\ \" \$ for \ " $, and expands
    # nothing else -- checked on systemd 249.
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//\$/\\\$}
    printf '%s="%s"\n' "$key" "$value"
  done
}

llm_unit() {
  cat <<EOF
[Unit]
Description=Ambarella LLM demo daemon (vendor), port 8080
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$LLM_DIR
Environment=$(q "PATH=$PATH")
ExecStart=$(q "$LLM_DIR/run_llm_demo.sh") --run_mode start --model_type $(q "$LLM_MODEL_TYPE") --model_path $(q "$LLM_MODEL_PATH") --ip 127.0.0.1 --max_user $(q "$LLM_MAX_USER")
ExecStop=$(q "$LLM_DIR/run_llm_demo.sh") --run_mode stop
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
EOF
}

qwen_unit() {
  cat <<EOF
[Unit]
Description=Qwen3.5-4B on vLLM (cooper-vllm), port 8000: the LLM every board graph asks
After=network-online.target ambarella-llm.service
Wants=network-online.target
# One model on the NPU at a time: starting either one stops the other, the
# stop ordered before the start (After=).
Conflicts=ambarella-llm.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$HOME
Environment=$(q "HOME=$HOME")
Environment=$(q "PATH=$PATH")
# The process appends to ~/workspace/vllm_qwen35.log itself, not systemd
# (see ten-api.service).
ExecStart=$(q "$SERVE") exec
# Started when /v1/models answers, about 55 s in: what is ordered after this
# unit, ten-api, starts once the LLM can answer.
ExecStartPost=$(q "$SERVE") ready 300
TimeoutStartSec=330
TimeoutStopSec=30
# Back whatever ended it -- a vLLM whose engine died may exit 0 -- but not
# after a stop: systemctl stop and a Conflicts= stop leave it stopped.
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
}

api_unit() {
  cat <<EOF
[Unit]
Description=TEN API server: meeting minutes and the voice assistant ($SERVER)
After=network-online.target qwen35-vllm.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$EXAMPLE
Environment=$(q "HOME=$HOME")
Environment=$(q "PATH=$PATH")
EnvironmentFile=$ENV_OUT
# The shell, running as $RUN_USER, opens the log -- not systemd: with
# fs.protected_regular, root may not open another user's file in /tmp for
# append, and the service died before it ran (status 209/STDOUT).
ExecStart=/bin/sh -c $(q "exec '$TASK' run-api-server >> '$TASK_LOG' 2>&1")
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

screen_unit() {
  local showcase=""
  [[ -n "$SHOWCASE" ]] && showcase=" --showcase '$SHOWCASE'"
  cat <<EOF
[Unit]
Description=Meeting demo screen: the board's meetings, read from disk, on 127.0.0.1:$SCREEN_PORT
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$SCREEN_DIR
Environment=$(q "HOME=$HOME")
# The process appends to the log itself, not systemd (see ten-api.service).
ExecStart=/bin/sh -c $(q "exec '$SCREEN_PY' -m meeting_screen --meetings-dir '$MEETINGS_DIR' --listen 127.0.0.1:$SCREEN_PORT$showcase >> '$SCREEN_LOG' 2>&1")
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

status() {
  local enabled active now qwen_up api_start
  for unit in qwen35-vllm ambarella-llm ten-api meeting-screen; do
    # Both print the state even when they exit non-zero (disabled, inactive).
    enabled=$(systemctl is-enabled "$unit" 2>/dev/null)
    active=$(systemctl is-active "$unit" 2>/dev/null)
    printf '  %-14s enabled: %-9s active: %s\n' "$unit" "${enabled:-no}" "${active:-no}"
  done
  now=$("$SERVE" which 2>/dev/null)
  case "$now" in
    qwen) ok "the LLM: Qwen3.5-4B answers" ;;
    7b) note "the LLM: the DeepSeek 7B daemon serves, not Qwen3.5-4B, which every board graph asks; serve_qwen35.sh start switches back" ;;
    *) note "the LLM: none answers (Qwen3.5-4B loads for about a minute after boot)" ;;
  esac
  if [[ $now == qwen ]] && "$SERVE" stale 2>/dev/null; then
    note "vLLM runs on older arguments than serve_qwen35.sh's: run this again, or serve_qwen35.sh restart"
  fi
  # At boot ten-api waits for Qwen3.5-4B (After= qwen35-vllm, whose start ends
  # when it answers). Both in microseconds since boot.
  qwen_up=$(systemctl show qwen35-vllm -p ActiveEnterTimestampMonotonic --value 2>/dev/null)
  api_start=$(systemctl show ten-api -p ExecMainStartTimestampMonotonic --value 2>/dev/null)
  if [[ "${qwen_up:-0}" =~ ^[1-9][0-9]*$ && "${api_start:-0}" =~ ^[1-9][0-9]*$ ]]; then
    if ((api_start >= qwen_up)); then
      ok "since boot: Qwen3.5-4B up at $((qwen_up / 1000000)) s, ten-api started at $((api_start / 1000000)) s"
    else
      note "since boot: ten-api started at $((api_start / 1000000)) s, before Qwen3.5-4B was up at $((qwen_up / 1000000)) s -- one of them was started again by hand"
    fi
  fi
  if answers "$SERVER/graphs"; then ok "API server answers at $SERVER"; else note "nothing answers at $SERVER"; fi
  if answers "$SCREEN/api/view"; then ok "demo screen answers at $SCREEN"; else note "the demo screen does not answer at $SCREEN"; fi
  if [[ -f "$AUTOSTART" ]]; then
    ok "kiosk: the screen opens when the desktop starts"
  else
    note "kiosk: off -- tools/ambarella/install_board_services.sh --kiosk on"
  fi
  if answers "$UPLOADER/meetings"; then
    note "a meeting worker is up ($UPLOADER answers): a meeting is in progress or was just served"
  fi
}

if [[ $MODE == status ]]; then
  status
  exit 0
fi

if [[ $MODE == kiosk ]]; then
  # Before the install's own check: with sudo, the entry would go to
  # root's desktop, which nobody logs into.
  [[ $(id -u) -ne 0 ]] || fail "run it as the desktop user (${SUDO_USER:-lychee}), not with sudo"
  if [[ $KIOSK == off ]]; then
    rm -f "$AUTOSTART"
    ok "the demo screen no longer opens with the desktop"
    exit 0
  fi
  [[ -x "$KIOSK_SCRIPT" ]] || fail "no $KIOSK_SCRIPT"
  mkdir -p "$(dirname "$AUTOSTART")"
  cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Meeting demo screen
Comment=The meeting demo screen, full screen; install_board_services.sh --kiosk off removes this
Exec=$KIOSK_SCRIPT
X-GNOME-Autostart-enabled=true
EOF
  ok "the demo screen opens full screen when $RUN_USER's desktop starts ($AUTOSTART)"
  note "now: log out and in again, or reboot; Alt+F4 leaves it"
  note "after a power cut it needs someone logged in, unless tools/ambarella/set_desktop_autologin.sh is on"
  exit 0
fi

if [[ $MODE == uninstall ]]; then
  sudo systemctl disable --now meeting-screen ten-api qwen35-vllm ambarella-llm 2>/dev/null
  sudo rm -f "$UNIT_DIR/meeting-screen.service" "$UNIT_DIR/ten-api.service" "$UNIT_DIR/qwen35-vllm.service" "$UNIT_DIR/ambarella-llm.service"
  rm -f "$AUTOSTART"
  sudo systemctl daemon-reload
  ok "removed; start by hand again as board_quickstart.md says"
  exit 0
fi

[[ -n "$TASK" && -x "$TASK" ]] || fail "task is not on PATH"
[[ -x "$SERVER_DIR/bin/api" ]] ||
  fail "no server/bin/api: run ai_agents/agents/scripts/install_board_arm64.sh first"
[[ -x "$LLM_DIR/run_llm_demo.sh" ]] || fail "no $LLM_DIR/run_llm_demo.sh: is the vendor's LLM demo installed?"
[[ -d "$LLM_MODEL_PATH" ]] || fail "no model at $LLM_MODEL_PATH (LLM_MODEL_PATH=... picks another)"
[[ -x "$QWEN_VLLM" ]] || fail "Qwen3.5-4B is not installed ($QWEN_VLLM): tools/ambarella/install_qwen35_vllm.sh"
[[ -x "$SERVE" ]] || fail "no $SERVE"
[[ -f "$SCREEN_DIR/meeting_screen/server.py" ]] || fail "no $SCREEN_DIR/meeting_screen: is this checkout up to date?"
[[ -x "$SCREEN_PY" ]] || fail "no $SCREEN_PY: the demo screen runs on the board's own python3"

if [[ $MODE == print ]]; then
  echo "# $ENV_OUT (variable names; values are written only on install)"
  environment | cut -d= -f1 | tr '\n' ' '
  echo
  echo
  echo "# $UNIT_DIR/qwen35-vllm.service"
  qwen_unit
  echo
  echo "# $UNIT_DIR/ambarella-llm.service"
  llm_unit
  echo
  echo "# $UNIT_DIR/meeting-screen.service"
  screen_unit
  echo
  echo "# $UNIT_DIR/ten-api.service"
  api_unit
  exit 0
fi

[[ $(id -u) -ne 0 ]] || fail "run this as the user who runs task run, not with sudo"

# The service appends to $TASK_LOG as $RUN_USER.
if [[ -e "$TASK_LOG" && ! -w "$TASK_LOG" ]]; then
  sudo chown "$RUN_USER" "$TASK_LOG" || fail "$TASK_LOG is not $RUN_USER's to write"
fi
if [[ -e "$SCREEN_LOG" && ! -w "$SCREEN_LOG" ]]; then
  sudo chown "$RUN_USER" "$SCREEN_LOG" || fail "$SCREEN_LOG is not $RUN_USER's to write"
fi

why_not() {
  echo "  -- systemctl status $1"
  systemctl status "$1" --no-pager -n 0 2>&1 | sed 's/^/     /'
  echo "  -- journalctl -u $1"
  sudo journalctl -u "$1" -n 15 --no-pager 2>&1 | sed 's/^/     /'
}

echo "== units"
# What ten-api runs with now, to tell whether it must restart to pick up a change.
was=$(sudo cat "$UNIT_DIR/ten-api.service" "$ENV_OUT" 2>/dev/null | md5sum)
tmp=$(mktemp -d)
qwen_unit > "$tmp/qwen35-vllm.service"
llm_unit > "$tmp/ambarella-llm.service"
api_unit > "$tmp/ten-api.service"
screen_unit > "$tmp/meeting-screen.service"
environment > "$tmp/ten-api.env"
nvars=$(grep -c . "$tmp/ten-api.env")
sudo install -m 600 -o root -g root "$tmp/ten-api.env" "$ENV_OUT" ||
  fail "could not write $ENV_OUT"
sudo install -m 644 "$tmp/qwen35-vllm.service" "$tmp/ambarella-llm.service" "$tmp/ten-api.service" "$tmp/meeting-screen.service" "$UNIT_DIR/" ||
  fail "could not write $UNIT_DIR"
rm -rf "$tmp"
sudo systemctl daemon-reload
sudo systemctl enable qwen35-vllm ten-api meeting-screen >/dev/null 2>&1 || fail "systemctl enable failed"
sudo systemctl disable ambarella-llm >/dev/null 2>&1
ok "qwen35-vllm, ten-api and meeting-screen installed in $UNIT_DIR and enabled at boot; ambarella-llm installed, not started at boot"
ok "ten-api runs with this terminal's environment: $nvars variables in $ENV_OUT"
changed=0
[[ "$(sudo cat "$UNIT_DIR/ten-api.service" "$ENV_OUT" 2>/dev/null | md5sum)" != "$was" ]] && changed=1

echo "== LLM"
now=$("$SERVE" which)
if answers "$UPLOADER/meetings"; then
  note "a meeting worker is up: the board keeps its LLM ($now) for now, so that its meeting is not cut short; Qwen3.5-4B serves from the next boot, or run this again when it is idle"
elif [[ $now == qwen ]] && systemctl is-active --quiet qwen35-vllm && ! "$SERVE" stale; then
  ok "Qwen3.5-4B answers, run by qwen35-vllm"
elif [[ $now == qwen ]]; then
  # Started by hand, or on older arguments: the service takes over.
  "$SERVE" restart || fail "Qwen3.5-4B did not come back under qwen35-vllm (above)"
else
  "$SERVE" start || fail "Qwen3.5-4B did not start under qwen35-vllm (above)"
fi

echo "== API server"
# Its Go code newer than the binary: a pull brought a change.
newer=$(find "$SERVER_DIR" -name '*.go' -newer "$SERVER_DIR/bin/api" -not -name '*_test.go' | head -3)
[[ "$SERVER_DIR/go.mod" -nt "$SERVER_DIR/bin/api" ]] && newer+=" go.mod"
if [[ -n "${newer// /}" ]]; then
  echo "  server code changed since bin/api was built: $(echo $newer | sed "s|$SERVER_DIR/||g")"
  command -v go >/dev/null || fail "go is not on PATH; it is needed to build server/bin/api"
  (cd "$SERVER_DIR" && go build -o bin/api.new main.go && mv bin/api.new bin/api) ||
    fail "go build failed; bin/api is the old one"
  ok "server/bin/api built again"
fi
# The service started before bin/api was last built, or before its unit or
# environment changed: it runs on the old ones.
if systemctl is-active --quiet ten-api; then
  since=$(date -d "$(systemctl show ten-api -p ActiveEnterTimestamp --value)" +%s 2>/dev/null || echo 0)
  if (( changed )) || (( $(stat -c %Y "$SERVER_DIR/bin/api") > since )); then
    if answers "http://127.0.0.1:8765/meetings"; then
      note "a meeting worker is up: ten-api keeps the old build and environment for now; run this again when it is idle"
    else
      sudo systemctl restart ten-api || { why_not ten-api; fail "ten-api did not restart"; }
      ok "ten-api restarted on the new build and environment"
    fi
  fi
fi
if ! systemctl is-active --quiet ten-api; then
  hand=$(pids_of 'bin/api -tenapp_dir')
  if [[ -n "$hand" ]]; then
    echo "  stopping the server started by hand: $(echo $hand)"
    kill $hand 2>/dev/null
    sleep 3
    # Its workers outlive it; the service's server would not know them.
    workers=$(pids_of '/tmp/ten_agent/property-')
    [[ -n "$workers" ]] && kill $workers 2>/dev/null && sleep 2
  fi
  # An earlier unit that kept failing may have hit systemd's start limit.
  sudo systemctl reset-failed ten-api 2>/dev/null
  sudo systemctl start ten-api || { why_not ten-api; fail "ten-api did not start"; }
fi
for _ in $(seq 30); do
  answers "$SERVER/graphs" && break
  sleep 1
done
if ! answers "$SERVER/graphs"; then
  why_not ten-api
  echo "  -- tail $TASK_LOG"
  tail -5 "$TASK_LOG" 2>&1 | sed 's/^/     /'
  fail "ten-api is installed but $SERVER does not answer"
fi
ok "API server answers at $SERVER, run by the ten-api service"

echo "== demo screen"
# Restarted every time: it reads the checkout's code, and only the screen
# notices -- for a second or two it says it is reconnecting.
sudo systemctl restart meeting-screen || { why_not meeting-screen; fail "meeting-screen did not start"; }
for _ in $(seq 20); do
  answers "$SCREEN/api/view" && break
  sleep 0.5
done
if ! answers "$SCREEN/api/view"; then
  why_not meeting-screen
  echo "  -- tail $SCREEN_LOG"
  tail -5 "$SCREEN_LOG" 2>&1 | sed 's/^/     /'
  fail "meeting-screen is installed but $SCREEN does not answer"
fi
ok "demo screen answers at $SCREEN, reading $MEETINGS_DIR${SHOWCASE:+, showcase $SHOWCASE}"

echo
status
echo
echo "From now on Qwen3.5-4B, the API server and the demo screen's service start at power-on (the 7B does not). To check after a reboot:"
echo "  tools/ambarella/install_board_services.sh --status"
[[ -f "$AUTOSTART" ]] || echo "The screen itself, full screen when the desktop starts: tools/ambarella/install_board_services.sh --kiosk on"
