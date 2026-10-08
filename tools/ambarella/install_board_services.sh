#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Make the board serve from power-on with nothing to type afterwards: two
# systemd services, enabled and started. Run once.
#
#   tools/ambarella/install_board_services.sh              install, enable, start
#   git pull --ff-only && tools/ambarella/install_board_services.sh   update
#   tools/ambarella/install_board_services.sh --status     is each one up
#   tools/ambarella/install_board_services.sh --print      the unit files; nothing changes
#   tools/ambarella/install_board_services.sh --uninstall  stop, disable, remove
#
#   ambarella-llm  the vendor's LLM daemon on 8080: run_llm_demo.sh --run_mode
#                  start at boot and stop at shutdown, run from its own
#                  directory so its log stays /tmp/log.txt; as root, which is
#                  what its processes run as anyway
#   ten-api        the Go API server on SERVER_PORT (8081): `task
#                  run-api-server` in examples/voice-assistant, as you, with
#                  this terminal's environment (written to /etc/ten-api.env,
#                  readable by root only) -- the same .env and environment as
#                  a run by hand;
#                  restarted if it dies; its output appended to
#                  /tmp/task_run.log, where the checks look
#
# With these up, any phone can ask for meeting minutes at any time: the
# server starts the meeting worker for the request, the record stays on the
# board's disk, and the board reaps the idle worker.
#
# A server started by hand (task run) holds the port, so it is stopped and the
# service takes over -- the playground stops with it; `task run-frontend` in
# examples/voice-assistant brings it back. An LLM daemon already running is
# left as it is; the service takes it over at the next boot.
#
# Run it again after every git pull: when the API server's Go code changed,
# server/bin/api is built again and ten-api restarted -- but not while a
# meeting worker is up, so a meeting being processed is not cut short; it
# then says to run this again later.
#
# Run it as the user who runs `task run`, not with sudo: sudo asks for your
# password for systemctl and /etc/systemd/system. The LLM daemon's options
# follow board_quickstart.md and can be changed through the environment:
#   LLM_MODEL_TYPE (9), LLM_MODEL_PATH (~/demo_resources/llm_demo), LLM_MAX_USER (1)

set -uo pipefail

MODE=install
case "${1:-}" in
  "") ;;
  --status) MODE=status ;;
  --print) MODE=print ;;
  --uninstall) MODE=uninstall ;;
  -h|--help) sed -n '6,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
EXAMPLE="$REPO_ROOT/ai_agents/agents/examples/voice-assistant"
SERVER_DIR="$REPO_ROOT/ai_agents/server"
ENV_FILE="$REPO_ROOT/ai_agents/.env"
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

api_unit() {
  cat <<EOF
[Unit]
Description=TEN API server: meeting minutes and the voice assistant ($SERVER)
After=network-online.target ambarella-llm.service
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
ExecStart=/bin/sh -c $(q "exec '$TASK' run-api-server >> /tmp/task_run.log 2>&1")
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

status() {
  for unit in ambarella-llm ten-api; do
    printf '  %-14s enabled: %-9s active: %s\n' "$unit" \
      "$(systemctl is-enabled "$unit" 2>/dev/null || echo no)" \
      "$(systemctl is-active "$unit" 2>/dev/null || echo no)"
  done
  if pgrep -x 'test_llm|test_llm_client' >/dev/null; then
    ok "LLM daemon processes running ($(pgrep -x 'test_llm|test_llm_client' | wc -l))"
  else
    note "no LLM daemon process (test_llm) running"
  fi
  if answers "$SERVER/graphs"; then ok "API server answers at $SERVER"; else note "nothing answers at $SERVER"; fi
  if answers "http://127.0.0.1:8765/meetings"; then
    note "a meeting worker is up (8765 answers): a meeting is in progress or was just served"
  fi
}

if [[ $MODE == status ]]; then
  status
  exit 0
fi

if [[ $MODE == uninstall ]]; then
  sudo systemctl disable --now ten-api ambarella-llm 2>/dev/null
  sudo rm -f "$UNIT_DIR/ten-api.service" "$UNIT_DIR/ambarella-llm.service"
  sudo systemctl daemon-reload
  ok "removed; start by hand again as board_quickstart.md says"
  exit 0
fi

[[ -n "$TASK" && -x "$TASK" ]] || fail "task is not on PATH"
[[ -x "$SERVER_DIR/bin/api" ]] ||
  fail "no server/bin/api: run ai_agents/agents/scripts/install_board_arm64.sh first"
[[ -x "$LLM_DIR/run_llm_demo.sh" ]] || fail "no $LLM_DIR/run_llm_demo.sh: is the vendor's LLM demo installed?"
[[ -d "$LLM_MODEL_PATH" ]] || fail "no model at $LLM_MODEL_PATH (LLM_MODEL_PATH=... picks another)"

if [[ $MODE == print ]]; then
  echo "# $ENV_OUT (variable names; values are written only on install)"
  environment | cut -d= -f1 | tr '\n' ' '
  echo
  echo
  echo "# $UNIT_DIR/ambarella-llm.service"
  llm_unit
  echo
  echo "# $UNIT_DIR/ten-api.service"
  api_unit
  exit 0
fi

[[ $(id -u) -ne 0 ]] || fail "run this as the user who runs task run, not with sudo"

# The service appends to /tmp/task_run.log as $RUN_USER.
if [[ -e /tmp/task_run.log && ! -w /tmp/task_run.log ]]; then
  sudo chown "$RUN_USER" /tmp/task_run.log || fail "/tmp/task_run.log is not $RUN_USER's to write"
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
llm_unit > "$tmp/ambarella-llm.service"
api_unit > "$tmp/ten-api.service"
environment > "$tmp/ten-api.env"
nvars=$(grep -c . "$tmp/ten-api.env")
sudo install -m 600 -o root -g root "$tmp/ten-api.env" "$ENV_OUT" ||
  fail "could not write $ENV_OUT"
sudo install -m 644 "$tmp/ambarella-llm.service" "$tmp/ten-api.service" "$UNIT_DIR/" ||
  fail "could not write $UNIT_DIR"
rm -rf "$tmp"
sudo systemctl daemon-reload
sudo systemctl enable ambarella-llm ten-api >/dev/null 2>&1 || fail "systemctl enable failed"
ok "ambarella-llm and ten-api installed in $UNIT_DIR and enabled at boot"
ok "ten-api runs with this terminal's environment: $nvars variables in $ENV_OUT"
changed=0
[[ "$(sudo cat "$UNIT_DIR/ten-api.service" "$ENV_OUT" 2>/dev/null | md5sum)" != "$was" ]] && changed=1

echo "== LLM daemon"
if pgrep -x 'test_llm|test_llm_client' >/dev/null; then
  note "already running, started by hand: left as it is; the service runs it from the next boot"
else
  sudo systemctl start ambarella-llm || { why_not ambarella-llm; fail "ambarella-llm did not start"; }
  for _ in $(seq 150); do
    grep -q "Device ENABLE" /tmp/log.txt 2>/dev/null && break
    sleep 1
  done
  if grep -q "Device ENABLE" /tmp/log.txt 2>/dev/null; then
    ok "LLM daemon up (Device ENABLE in /tmp/log.txt)"
  else
    note "started, but no Device ENABLE in /tmp/log.txt after 150 s; the first load can be slow"
  fi
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
  echo "  -- tail /tmp/task_run.log"
  tail -5 /tmp/task_run.log 2>&1 | sed 's/^/     /'
  fail "ten-api is installed but $SERVER does not answer"
fi
ok "API server answers at $SERVER, run by the ten-api service"

echo
status
echo
echo "From now on both start at power-on. To check after a reboot:"
echo "  tools/ambarella/install_board_services.sh --status"
