#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Switch the board's LLM between the vendor's DeepSeek 7B daemon
# (ambarella-llm, port 8080, what the meeting graph uses) and Qwen3.5-4B on
# cooper-vllm (port 8000). The two cannot run at the same time.
#
#   tools/ambarella/serve_qwen35.sh start    stop the 7B daemon, start Qwen3.5-4B,
#                                            wait until it answers
#   tools/ambarella/serve_qwen35.sh restart  stop and start Qwen3.5-4B, the 7B left as it is
#   tools/ambarella/serve_qwen35.sh test     one meeting-style question, asked as
#                                            the graphs ask (no chat_template_kwargs):
#                                            thinking must be off by default; the
#                                            answer, seconds, tokens/s
#   tools/ambarella/serve_qwen35.sh stop     stop Qwen3.5-4B; start the 7B daemon
#                                            again if start stopped it
#   tools/ambarella/serve_qwen35.sh status   which one is up
#
# Install first: tools/ambarella/install_qwen35_vllm.sh. Run it as the desktop
# user (lychee), not with sudo; it asks sudo only to stop and start the
# ambarella-llm service.
#
# start refuses while a meeting worker is up (8765 answers): its meeting would
# lose the LLM halfway. vLLM runs in a session of its own, so it outlives the
# SSH login it was started from; its output goes to ~/workspace/vllm_qwen35.log.
# When it fails to come up, start stops it again and gives the 7B back.
#
# Settings, from the environment: WORK (~/workspace), QWEN_PKG
# (~/demo_resources/llm_demo/qwen3.5_4B), VIT_NAME, SHEPHERD_DIR
# (~/workspace/shepherd-1.6.9, used only when it exists), QWEN_PORT (8000),
# QWEN_WAIT_S (900), LLM_DIR (/usr/share/ambarella/llm_demo).

set -uo pipefail

WORK="${WORK:-$HOME/workspace}"
VENV="${VENV:-$WORK/cooper-vllm/venv_vllm}"
# The package: as set; else where install_qwen35_vllm.sh unpacked it; else
# the guide's place.
if [[ -z "${QWEN_PKG:-}" ]]; then
  QWEN_PKG=$(cat "$WORK/.qwen35-install/qwen_pkg" 2>/dev/null)
  [[ -n "$QWEN_PKG" && -d "$QWEN_PKG/ranch" ]] || QWEN_PKG="$HOME/demo_resources/llm_demo/qwen3.5_4B"
fi
MODEL="$QWEN_PKG/ranch"
VIT_NAME="${VIT_NAME:-qwen3.5_4b_vl_vit_384x384_n1655_a16w8_cavalry.bin}"
SHEPHERD_DIR="${SHEPHERD_DIR:-$WORK/shepherd-1.6.9}"
PORT="${QWEN_PORT:-8000}"
WAIT_S="${QWEN_WAIT_S:-900}"
LLM_DIR="${LLM_DIR:-/usr/share/ambarella/llm_demo}"
NAME="qwen3.5_4B"
API="http://127.0.0.1:$PORT"
RUN="$WORK/.qwen35-serve"
LOG="$WORK/vllm_qwen35.log"
DAEMON_LOG="${DAEMON_LOG:-/tmp/log.txt}"
GIVE_BACK_S="${GIVE_BACK_S:-300}"

ok() { echo "  ok    $*"; }
note() { echo "  note  $*"; }
fail() { echo "  FAIL  $*"; exit 1; }

serving() {
  [[ $(curl -s -m 5 -o /dev/null -w '%{http_code}' \
    -H 'Authorization: Bearer EMPTY' "$API/v1/models") == 200 ]]
}
daemon_up() { pgrep -x test_llm >/dev/null; }
worker_up() { [[ $(curl -s -m 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/meetings) != 000 ]]; }
vllm_pid() {
  local pid
  pid=$(cat "$RUN/pid" 2>/dev/null) || return 1
  kill -0 "$pid" 2>/dev/null && echo "$pid"
}
# Where the daemon's log stands: its inode and size. What comes after is a
# new start's. Whether the daemon appends to the log or writes it anew is not
# known; a log that is another file now, or shorter, is read from its start.
log_mark() { stat -c '%i %s' "$DAEMON_LOG" 2>/dev/null || echo "0 0"; }
enabled_since() { # "inode size" from log_mark
  local then_inode then_size now_inode now_size
  read -r then_inode then_size <<< "$1"
  read -r now_inode now_size <<< "$(log_mark)"
  if [[ "$now_inode" != "$then_inode" ]] || ((now_size < then_size)); then
    grep -q 'Device ENABLE' "$DAEMON_LOG" 2>/dev/null
  else
    tail -c +"$((then_size + 1))" "$DAEMON_LOG" 2>/dev/null | grep -q 'Device ENABLE'
  fi
}
# Whether something listens on a port: ss where there is one, else the
# kernel's own table (state 0A is LISTEN).
listening() {
  if command -v ss >/dev/null; then
    ss -ltn 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {found = 1} END {exit !found}'
  else
    awk -v h=":$(printf '%04X' "$1")" 'FNR > 1 && $2 ~ h"$" && $4 == "0A" {found = 1} END {exit !found}' \
      /proc/net/tcp /proc/net/tcp6 2>/dev/null
  fi
}

stop_daemon() {
  daemon_up || return 0
  if systemctl is-active --quiet ambarella-llm; then
    echo service > "$RUN/llm_was_up"
    sudo systemctl stop ambarella-llm || fail "could not stop ambarella-llm"
  else
    # Started by hand. Given back through the service, if there is one.
    echo hand > "$RUN/llm_was_up"
    [[ -x "$LLM_DIR/run_llm_demo.sh" ]] || fail "the 7B daemon runs, but $LLM_DIR/run_llm_demo.sh is not there to stop it"
    (cd "$LLM_DIR" && sudo ./run_llm_demo.sh --run_mode stop) >/dev/null 2>&1
  fi
  for _ in $(seq 30); do
    daemon_up || break
    sleep 1
  done
  daemon_up && fail "the 7B daemon (test_llm) is still running"
  ok "stopped the DeepSeek 7B daemon"
}

give_back_daemon() {
  [[ -f "$RUN/llm_was_up" ]] || return 0
  if daemon_up; then
    rm -f "$RUN/llm_was_up"
    return 0
  fi
  if ! systemctl cat ambarella-llm >/dev/null 2>&1; then
    note "the 7B daemon was started by hand and there is no ambarella-llm service: start it again as you did"
    rm -f "$RUN/llm_was_up"
    return 0
  fi
  local mark ready=0
  mark=$(log_mark)
  sudo systemctl start ambarella-llm || fail "could not start ambarella-llm again"
  for _ in $(seq "$GIVE_BACK_S"); do
    if daemon_up && listening 8080 && enabled_since "$mark"; then
      ready=1
      break
    fi
    sleep 1
  done
  rm -f "$RUN/llm_was_up"
  if ((ready)); then
    ok "the DeepSeek 7B daemon is back: test_llm runs, 8080 listens, a new Device ENABLE in $DAEMON_LOG"
    return 0
  fi
  # What did and did not happen, so that the next look need not guess.
  note "ambarella-llm was started, but is not seen ready after $GIVE_BACK_S s:"
  note "  test_llm running: $(daemon_up && echo yes || echo no); 8080 listening: $(listening 8080 && echo yes || echo no); new Device ENABLE: $(enabled_since "$mark" && echo yes || echo no)"
  note "  log $DAEMON_LOG, before: inode/size $mark; now: $(log_mark); its last lines:"
  tail -3 "$DAEMON_LOG" 2>/dev/null | sed 's/^/          /'
  note "  check again later: tools/ambarella/install_board_services.sh --status"
}

# vLLM alone: the 7B is left as it is, and so is the note that it was taken.
stop_vllm() {
  local pid
  if pid=$(vllm_pid); then
    # vLLM's engine runs in processes of its own; the session holds them all.
    local all
    all=$(ps -o pid= -s "$pid" 2>/dev/null | tr '\n' ' ')
    kill -TERM $all 2>/dev/null
    for _ in $(seq 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    all=$(ps -o pid= -s "$pid" 2>/dev/null | tr '\n' ' ')
    [[ -n "${all// /}" ]] && kill -KILL $all 2>/dev/null
    ok "stopped Qwen3.5-4B"
  fi
  rm -f "$RUN/pid"
  serving && fail "something still answers at $API that this script did not start"
  return 0
}

do_stop() {
  stop_vllm
  give_back_daemon
}

# A template changed under a running vLLM is read only when it starts again.
# What start would refuse is refused before the stop, not after it.
do_restart() {
  [[ $(id -u) -ne 0 ]] || fail "run it as the desktop user (lychee), not with sudo"
  if worker_up; then
    fail "a meeting worker is up (8765 answers): its meeting would lose the LLM. Let the board reap it, or run tools/ambarella/clear_meeting_worker.sh"
  fi
  stop_vllm
  do_start
}

do_start() {
  [[ $(id -u) -ne 0 ]] || fail "run it as the desktop user (lychee), not with sudo"
  [[ -x "$VENV/bin/vllm" ]] || fail "no $VENV/bin/vllm: run tools/ambarella/install_qwen35_vllm.sh first"
  [[ -f "$MODEL/config.json" ]] || fail "no $MODEL/config.json: run tools/ambarella/install_qwen35_vllm.sh first"
  mkdir -p "$RUN"
  if serving; then
    ok "Qwen3.5-4B already answers at $API"
    return 0
  fi
  if worker_up; then
    fail "a meeting worker is up (8765 answers): its meeting would lose the LLM. Let the board reap it, or run tools/ambarella/clear_meeting_worker.sh"
  fi
  vllm_pid >/dev/null && fail "Qwen3.5-4B is starting already (pid $(vllm_pid)); serve_qwen35.sh stop first"
  stop_daemon

  local lib=""
  [[ -e "$SHEPHERD_DIR/libamba-shepherd-n1_655.so.1" ]] && lib="$SHEPHERD_DIR"
  echo "== $(date '+%F %T') serve_qwen35.sh start" >> "$LOG"
  (
    export PATH="$VENV/bin:$PATH" VIRTUAL_ENV="$VENV"
    export AMBARELLA_ARCH=n1_655 AMBA_EXT_VLM_VIT_NAME="$VIT_NAME"
    export VLLM_CONFIG_ROOT=/tmp/ VLLM_CPU_OMP_THREADS_BIND=all VLLM_USE_V1=0 VLLM_NO_NUMA=1
    [[ -n "$lib" ]] && export LD_LIBRARY_PATH="$lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    exec setsid "$VENV/bin/vllm" serve "$MODEL" \
      --served-model-name "$NAME" \
      --host 127.0.0.1 --port "$PORT" --api-key EMPTY \
      --max-model-len 8192 \
      --model-impl transformers_amba_ext_vlm \
      --load-format dummy --swap-space 1 \
      --reasoning-parser qwen3 \
      --hf-overrides '{"vit_mode": 0, "model_type": "qwen3_5_text"}' \
      --trust-remote-code
  ) >> "$LOG" 2>&1 < /dev/null &
  echo $! > "$RUN/pid"
  ok "starting Qwen3.5-4B (pid $!)${lib:+ with Shepherd from $lib}; log $LOG"

  local waited=0
  while (( waited < WAIT_S )); do
    if serving; then
      ok "Qwen3.5-4B answers at $API after ${waited} s"
      return 0
    fi
    if ! vllm_pid >/dev/null; then
      echo "  -- tail $LOG"
      tail -20 "$LOG" | sed 's/^/     /'
      do_stop
      fail "vLLM exited before it answered (its log is above)"
    fi
    sleep 5
    waited=$((waited + 5))
    (( waited % 60 == 0 )) && note "still loading after ${waited} s: $(tail -1 "$LOG" | cut -c1-120)"
  done
  echo "  -- tail $LOG"
  tail -20 "$LOG" | sed 's/^/     /'
  do_stop
  fail "Qwen3.5-4B did not answer within $WAIT_S s; it is stopped again"
}

do_test() {
  serving || fail "Qwen3.5-4B is not answering at $API: serve_qwen35.sh start"
  python3 -I - "$API" "$NAME" <<'EOF'
import json, sys, time, urllib.request

api, name = sys.argv[1], sys.argv[2]
prompt = (
    "请用两句话总结下面这段会议记录。\n"
    "说话人1：下周三的发布会，场地已经订好了，但是宣传物料还没到。\n"
    "说话人2：物料周一能到，我来跟进供应商。\n"
    "说话人1：好，那彩排就放在周二下午，大家都要到。"
)
# No chat_template_kwargs: asked as openai_llm2_python asks, which cannot
# send them, thinking must be off by the template's default.
body = {
    "model": name,
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": 256,
    "temperature": 0,
}
req = urllib.request.Request(
    api + "/v1/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"},
)
start = time.monotonic()
try:
    with urllib.request.urlopen(req, timeout=900) as r:
        reply = json.load(r)
except Exception as err:  # pylint: disable=broad-except
    print(f"  FAIL  the request failed: {err}")
    sys.exit(1)
took = time.monotonic() - start
message = reply["choices"][0]["message"]
content = message.get("content") or ""
reasoning = message.get("reasoning_content") or ""
tokens = (reply.get("usage") or {}).get("completion_tokens") or 0
print("  answer:")
for line in content.strip().splitlines() or ["(empty)"]:
    print(f"        {line}")
print(f"  ok    {took:.1f} s, {tokens} tokens"
      + (f", {tokens / took:.1f} tokens/s" if took and tokens else ""))
bad = 0
if "<think>" in content or reasoning.strip():
    print("  FAIL  thinking is not off by default: run qwen35_prepare.py model "
          "on the package, then serve_qwen35.sh restart")
    bad = 1
if not content.strip():
    print("  FAIL  the answer is empty")
    bad = 1
if "\ufffd" in content:
    print(f"  FAIL  {content.count(chr(0xfffd))} U+FFFD in the answer")
    bad = 1
if not bad:
    print("  ok    thinking is off and the answer is clean")
sys.exit(bad)
EOF
}

do_status() {
  if daemon_up; then ok "DeepSeek 7B daemon running (test_llm)"; else note "DeepSeek 7B daemon not running"; fi
  if serving; then
    ok "Qwen3.5-4B answers at $API (pid $(vllm_pid || echo '?'))"
  elif vllm_pid >/dev/null; then
    note "Qwen3.5-4B is starting (pid $(vllm_pid)); tail -f $LOG"
  else
    note "Qwen3.5-4B not running"
  fi
  [[ -f "$RUN/llm_was_up" ]] && note "the 7B daemon was stopped for Qwen3.5-4B; stop gives it back"
  worker_up && note "a meeting worker is up (8765)"
  return 0
}

mkdir -p "$RUN"
case "${1:-}" in
  start) do_start ;;
  stop) do_stop ;;
  restart) do_restart ;;
  test) do_test ;;
  status) do_status ;;
  -h|--help|"") sed -n '6,33p' "$0" | sed 's/^# \{0,1\}//'; [[ -n "${1:-}" ]] ;;
  *) echo "unknown command: $1 (start, restart, test, stop, status)" >&2; exit 2 ;;
esac
