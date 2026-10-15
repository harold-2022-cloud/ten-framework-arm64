#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# The board's LLM: Qwen3.5-4B on cooper-vllm (port 8000), which every board
# graph asks, or the vendor's DeepSeek 7B daemon (ambarella-llm, port 8080),
# kept for the LLM comparison and as a way back. The two cannot run at the
# same time.
#
#   tools/ambarella/serve_qwen35.sh start       Qwen3.5-4B serves: the 7B stopped,
#                                               Qwen3.5-4B started; back when it answers
#   tools/ambarella/serve_qwen35.sh stop        the 7B serves: Qwen3.5-4B stopped, the
#                                               7B started (without the services: only
#                                               if start had stopped it)
#   tools/ambarella/serve_qwen35.sh restart     Qwen3.5-4B stopped and started, the 7B
#                                               left as it is: vLLM reads its template
#                                               and its arguments only when it starts
#   tools/ambarella/serve_qwen35.sh status      which one is up; whether vLLM runs on
#                                               this script's arguments
#   tools/ambarella/serve_qwen35.sh test        one meeting-style question, asked as the
#                                               graphs ask (no chat_template_kwargs):
#                                               thinking off by default, no U+FFFD
#   tools/ambarella/serve_qwen35.sh test-tools  a weather question with the weather
#                                               graphs' tools, streamed as they ask: a
#                                               tool call must come back
#   tools/ambarella/serve_qwen35.sh which       qwen, 7b or none: what serves now
#   tools/ambarella/serve_qwen35.sh back WHAT   the board put back to what `which` said
#                                               before a switch (none: left as it is)
#
# For the qwen35-vllm service (install_board_services.sh), not to be typed:
#   exec      vLLM in the foreground, its output appended to the log
#   ready N   back once Qwen3.5-4B answers; fails when vLLM exits first or
#             after N seconds
#   stale     exit 0 when the running vLLM was started on other arguments
#
# With the services installed, start, stop and restart go through systemctl
# (qwen35-vllm and ambarella-llm), so that the board never runs two. Without
# them, vLLM runs in a session of its own and outlives the SSH login.
#
# Run it as the desktop user (lychee), not with sudo; it asks sudo for
# systemctl. stop and restart refuse while a meeting worker is up (8765
# answers): every graph asks Qwen3.5-4B, and the meeting would lose it
# halfway. start does not: it gives the graphs their LLM back. vLLM's output
# goes to ~/workspace/vllm_qwen35.log; the requests themselves are not
# logged, or every meeting's transcript would be kept there for good.
#
# Settings, from the environment: WORK (~/workspace), QWEN_PKG
# (~/demo_resources/llm_demo/qwen3.5_4B), VIT_NAME, SHEPHERD_DIR
# (~/workspace/shepherd-1.6.9, used only when it exists), QWEN_PORT (8000),
# QWEN_WAIT_S (900), LLM_DIR (/usr/share/ambarella/llm_demo), LLM_PORT
# (8080), MEETING_UPLOADER (http://127.0.0.1:8765).

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
LLM_PORT="${LLM_PORT:-8080}"
UPLOADER="${MEETING_UPLOADER:-http://127.0.0.1:8765}"
NAME="qwen3.5_4B"
API="http://127.0.0.1:$PORT"
RUN="$WORK/.qwen35-serve"
LOG="$WORK/vllm_qwen35.log"
DAEMON_LOG="${DAEMON_LOG:-/tmp/log.txt}"
GIVE_BACK_S="${GIVE_BACK_S:-300}"
QWEN_UNIT="qwen35-vllm"
LLM_UNIT="ambarella-llm"

# vLLM's command line, in this one place: start runs it in the background,
# exec in the foreground for the qwen35-vllm service. The tool flags let a
# request carry tools -- the weather graphs' do -- which vLLM refuses
# without them.
VLLM_ARGS=(
  serve "$MODEL"
  --served-model-name "$NAME"
  --host 127.0.0.1 --port "$PORT" --api-key EMPTY
  --max-model-len 8192
  --model-impl transformers_amba_ext_vlm
  --load-format dummy --swap-space 1
  --reasoning-parser qwen3
  --enable-auto-tool-choice --tool-call-parser hermes
  --hf-overrides '{"vit_mode": 0, "model_type": "qwen3_5_text"}'
  --trust-remote-code
  --disable-log-requests
)

ok() { echo "  ok    $*"; }
note() { echo "  note  $*"; }
fail() { echo "  FAIL  $*"; exit 1; }

serving() {
  [[ $(curl -s -m 5 -o /dev/null -w '%{http_code}' \
    -H 'Authorization: Bearer EMPTY' "$API/v1/models") == 200 ]]
}
daemon_up() { pgrep -x test_llm >/dev/null; }
worker_up() { [[ $(curl -s -m 2 -o /dev/null -w '%{http_code}' "$UPLOADER/meetings") != 000 ]]; }
# When a process started, in clock ticks since boot: with its pid it names
# one process, where the pid alone may name another by now.
started_at() { sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | awk '{print $20}'; }
vllm_pid() {
  local pid
  pid=$(cat "$RUN/pid" 2>/dev/null) || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # A pid file that outlived its vLLM may name another process of the user's
  # -- a login shell, whose whole session stop_vllm would end.
  if [[ -f "$RUN/pid.start" && "$(started_at "$pid")" != "$(cat "$RUN/pid.start")" ]]; then
    return 1
  fi
  echo "$pid"
}
units() { systemctl cat "$QWEN_UNIT" >/dev/null 2>&1; }
# A vLLM this script started in the background, not the service's.
hand_vllm() {
  local pid main
  pid=$(vllm_pid) || return 1
  main=$(systemctl show -p MainPID --value "$QWEN_UNIT" 2>/dev/null)
  [[ "$pid" != "$main" ]]
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

not_root() { [[ $(id -u) -ne 0 ]] || fail "run it as the desktop user (lychee), not with sudo"; }
installed() {
  [[ -x "$VENV/bin/vllm" ]] || fail "no $VENV/bin/vllm: run tools/ambarella/install_qwen35_vllm.sh first"
  [[ -f "$MODEL/config.json" ]] || fail "no $MODEL/config.json: run tools/ambarella/install_qwen35_vllm.sh first"
}
no_worker() {
  if worker_up; then
    fail "a meeting worker is up ($UPLOADER answers): its meeting would lose the LLM. Let the board reap it, or run tools/ambarella/clear_meeting_worker.sh"
  fi
}

# The environment vLLM runs in, the same for start and exec; and the
# arguments it runs on, kept for stale.
vllm_env() {
  export PATH="$VENV/bin:$PATH" VIRTUAL_ENV="$VENV"
  export AMBARELLA_ARCH=n1_655 AMBA_EXT_VLM_VIT_NAME="$VIT_NAME"
  export VLLM_CONFIG_ROOT=/tmp/ VLLM_CPU_OMP_THREADS_BIND=all VLLM_USE_V1=0 VLLM_NO_NUMA=1
  if [[ -e "$SHEPHERD_DIR/libamba-shepherd-n1_655.so.1" ]]; then
    export LD_LIBRARY_PATH="$SHEPHERD_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
  printf '%s\n' "${VLLM_ARGS[@]}" > "$RUN/args"
}

stop_daemon() {
  daemon_up || return 0
  if systemctl is-active --quiet "$LLM_UNIT"; then
    echo service > "$RUN/llm_was_up"
    sudo systemctl stop "$LLM_UNIT" || fail "could not stop $LLM_UNIT"
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
  if ! systemctl cat "$LLM_UNIT" >/dev/null 2>&1; then
    note "the 7B daemon was started by hand and there is no $LLM_UNIT service: start it again as you did"
    rm -f "$RUN/llm_was_up"
    return 0
  fi
  local mark ready=0
  mark=$(log_mark)
  sudo systemctl start "$LLM_UNIT" || fail "could not start $LLM_UNIT again"
  for _ in $(seq "$GIVE_BACK_S"); do
    if daemon_up && listening "$LLM_PORT" && enabled_since "$mark"; then
      ready=1
      break
    fi
    sleep 1
  done
  rm -f "$RUN/llm_was_up"
  if ((ready)); then
    ok "the DeepSeek 7B daemon is back: test_llm runs, $LLM_PORT listens, a new Device ENABLE in $DAEMON_LOG"
    return 0
  fi
  # What did and did not happen, so that the next look need not guess.
  note "$LLM_UNIT was started, but is not seen ready after $GIVE_BACK_S s:"
  note "  test_llm running: $(daemon_up && echo yes || echo no); $LLM_PORT listening: $(listening "$LLM_PORT" && echo yes || echo no); new Device ENABLE: $(enabled_since "$mark" && echo yes || echo no)"
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
  rm -f "$RUN/pid" "$RUN/pid.start"
  serving && fail "something still answers at $API that this script did not start"
  return 0
}

show_unit() {
  echo "  -- systemctl status $QWEN_UNIT"
  systemctl status "$QWEN_UNIT" --no-pager -n 0 2>&1 | sed 's/^/     /'
  echo "  -- tail $LOG"
  tail -20 "$LOG" 2>/dev/null | sed 's/^/     /'
}

do_start() {
  not_root
  installed
  mkdir -p "$RUN"
  if serving; then
    ok "Qwen3.5-4B already answers at $API"
    return 0
  fi
  if units; then
    # The service runs it: a vLLM started by hand would hold the port, and
    # the 7B the NPU.
    hand_vllm && stop_vllm
    stop_daemon
    sudo systemctl start "$QWEN_UNIT" || { show_unit; fail "$QWEN_UNIT did not start"; }
    serving || { show_unit; fail "$QWEN_UNIT is up, but nothing answers at $API"; }
    ok "Qwen3.5-4B answers at $API, run by $QWEN_UNIT"
    return 0
  fi
  vllm_pid >/dev/null && fail "Qwen3.5-4B is starting already (pid $(vllm_pid)); serve_qwen35.sh stop first"
  stop_daemon

  local lib=""
  [[ -e "$SHEPHERD_DIR/libamba-shepherd-n1_655.so.1" ]] && lib="$SHEPHERD_DIR"
  echo "== $(date '+%F %T') serve_qwen35.sh start" >> "$LOG"
  (
    vllm_env
    exec setsid "$VENV/bin/vllm" "${VLLM_ARGS[@]}"
  ) >> "$LOG" 2>&1 < /dev/null &
  echo $! > "$RUN/pid"
  started_at $! > "$RUN/pid.start"
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
      switch_back
      fail "vLLM exited before it answered (its log is above)"
    fi
    sleep 5
    waited=$((waited + 5))
    (( waited % 60 == 0 )) && note "still loading after ${waited} s: $(tail -1 "$LOG" | cut -c1-120)"
  done
  echo "  -- tail $LOG"
  tail -20 "$LOG" | sed 's/^/     /'
  switch_back
  fail "Qwen3.5-4B did not answer within $WAIT_S s; it is stopped again"
}

do_stop() {
  no_worker
  switch_back
}

# Qwen3.5-4B stopped and the 7B given back, whatever runs: stop's work, and
# start's when Qwen3.5-4B does not come up.
switch_back() {
  if units; then
    # With the services, stop means the 7B: Qwen3.5-4B's service stopped,
    # the 7B's started and waited for.
    sudo systemctl stop "$QWEN_UNIT" || fail "could not stop $QWEN_UNIT"
    hand_vllm && stop_vllm
    mkdir -p "$RUN"
    echo service > "$RUN/llm_was_up"
    give_back_daemon
    return 0
  fi
  stop_vllm
  give_back_daemon
}

# A template or arguments changed under a running vLLM are read only when it
# starts again. What start would refuse is refused before the stop.
do_restart() {
  not_root
  installed
  no_worker
  mkdir -p "$RUN"
  if units; then
    hand_vllm && stop_vllm
    stop_daemon
    sudo systemctl restart "$QWEN_UNIT" || { show_unit; fail "$QWEN_UNIT did not start again"; }
    serving || { show_unit; fail "$QWEN_UNIT restarted, but nothing answers at $API"; }
    ok "Qwen3.5-4B restarted by $QWEN_UNIT; it answers at $API"
    return 0
  fi
  stop_vllm
  do_start
}

# The qwen35-vllm service's ExecStart: vLLM in the foreground, as the
# service's main process -- no setsid, which in a service (a session leader
# already) would fork and leave systemd watching a process that has exited.
# The output goes where start's goes, opened by this process: as root,
# systemd may not append to another user's file (fs.protected_regular).
do_exec() {
  not_root
  installed
  mkdir -p "$RUN"
  exec >> "$LOG" 2>&1 < /dev/null
  echo "== $(date '+%F %T') serve_qwen35.sh exec (pid $$)"
  echo $$ > "$RUN/pid"
  started_at $$ > "$RUN/pid.start"
  vllm_env
  exec "$VENV/bin/vllm" "${VLLM_ARGS[@]}"
}

# The qwen35-vllm service's ExecStartPost: back once Qwen3.5-4B answers, so
# that what is ordered after the service (ten-api) starts when it does.
do_ready() {
  local limit="${1:-$WAIT_S}" waited=0 pid
  [[ "$limit" =~ ^[0-9]+$ ]] || fail "ready takes a number of seconds"
  while ((waited < limit)); do
    if serving; then
      ok "Qwen3.5-4B answers at $API after ${waited} s"
      return 0
    fi
    # The service's main process; else the pid exec wrote, trusted once exec
    # has had time to write it, since the file may be an earlier run's.
    pid=${MAINPID:-}
    if [[ -z "$pid" ]] && ((waited >= 10)); then
      pid=$(cat "$RUN/pid" 2>/dev/null)
    fi
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
      tail -20 "$LOG" 2>/dev/null | sed 's/^/     /'
      fail "vLLM (pid $pid) exited before it answered; its log is above"
    fi
    sleep 2
    waited=$((waited + 2))
  done
  fail "Qwen3.5-4B did not answer within $limit s; $LOG"
}

do_which() {
  if serving; then
    echo qwen
  elif daemon_up; then
    echo 7b
  else
    echo none
  fi
}

# For a script that switches for a test: the board put back to what `which`
# said before it.
do_back() {
  case "${1:-}" in
    qwen)
      if serving; then ok "Qwen3.5-4B serves, as before"; else do_start; fi
      ;;
    7b)
      if daemon_up && ! serving; then ok "the DeepSeek 7B serves, as before"; else do_stop; fi
      ;;
    none) ok "nothing served before; left as it is" ;;
    *) fail "back takes what which said: qwen, 7b or none" ;;
  esac
}

do_stale() {
  [[ -f "$RUN/args" ]] && ! printf '%s\n' "${VLLM_ARGS[@]}" | cmp -s - "$RUN/args"
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
if chr(0xFFFD) in content:
    print(f"  FAIL  {content.count(chr(0xfffd))} U+FFFD in the answer")
    bad = 1
if not bad:
    print("  ok    thinking is off and the answer is clean")
sys.exit(bad)
EOF
}

# A weather question with the weather graphs' tools, as openai_llm2_python
# sends weatherapi_tool_python's: streamed, no tool_choice (vLLM: auto).
do_test_tools() {
  serving || fail "Qwen3.5-4B is not answering at $API: serve_qwen35.sh start"
  python3 -I - "$API" "$NAME" <<'EOF'
import json, sys, time, urllib.error, urllib.request

api, name = sys.argv[1], sys.argv[2]
LOCATION = "The city and state (use only English) e.g. San Francisco, CA"


def tool(fn, description, params):
    return {
        "type": "function",
        "function": {
            "name": fn,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {p: {"type": "string", "description": d} for p, d in params},
                "required": [p for p, _ in params],
                "additionalProperties": False,
            },
        },
        "strict": True,
    }


tools = [
    tool("get_current_weather", "Determine current weather in user's location.",
         [("location", LOCATION)]),
    tool("get_past_weather", "Determine weather within past 7 days in user's location.",
         [("location", LOCATION),
          ("datetime", "The datetime user is referring in date format e.g. 2024-10-09")]),
    tool("get_future_weather", "Determine weather in next 3 days in user's location.",
         [("location", LOCATION)]),
]
names = {t["function"]["name"] for t in tools}
body = {
    "model": name,
    "messages": [
        {"role": "system", "content": "用中文简短口语回答，不要条列或符号。"},
        {"role": "user", "content": "明天上海天气怎么样？"},
    ],
    "tools": tools,
    "stream": True,
    "n": 1,
    "max_tokens": 512,
    "temperature": 0.7,
    "frequency_penalty": 0.0,
}
req = urllib.request.Request(
    api + "/v1/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"},
)
start = time.monotonic()
content, calls = "", {}
try:
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            for choice in json.loads(line[5:]).get("choices") or []:
                delta = choice.get("delta") or {}
                content += delta.get("content") or ""
                for c in delta.get("tool_calls") or []:
                    call = calls.setdefault(c.get("index", 0), {"name": "", "arguments": ""})
                    fn = c.get("function") or {}
                    call["name"] += fn.get("name") or ""
                    call["arguments"] += fn.get("arguments") or ""
except urllib.error.HTTPError as err:
    detail = err.read().decode("utf-8", "replace")
    print(f"  FAIL  the request was refused: {err.code} {detail[:300]}")
    if "tool" in detail and "choice" in detail:
        print("        vLLM runs without --enable-auto-tool-choice: serve_qwen35.sh restart")
    sys.exit(1)
except Exception as err:  # pylint: disable=broad-except
    print(f"  FAIL  the request failed: {err}")
    sys.exit(1)
took = time.monotonic() - start
for _, call in sorted(calls.items()):
    print(f"  call  {call['name']}({call['arguments']})")
if content.strip():
    print(f"  text  {content.strip()[:200]}")
bad = 0
if "<tool_call>" in content:
    print("  FAIL  the tool call came back as text: the hermes parser did not take it")
    bad = 1
elif not calls:
    print("  FAIL  no tool call for a weather question")
    bad = 1
for call in calls.values():
    if call["name"] not in names:
        print(f"  FAIL  {call['name']!r} is not one of the tools asked")
        bad = 1
        continue
    try:
        location = (json.loads(call["arguments"] or "{}") or {}).get("location")
    except ValueError:
        location = None
    if not location:
        print(f"  FAIL  {call['name']} came without a location: {call['arguments']!r}")
        bad = 1
if "<think>" in content:
    print("  FAIL  the answer carries a <think> part")
    bad = 1
if not bad:
    print(f"  ok    a tool call in {took:.1f} s, streamed as the weather graphs ask")
sys.exit(bad)
EOF
}

do_status() {
  local unit
  if units; then
    for unit in "$QWEN_UNIT" "$LLM_UNIT"; do
      printf '  %-14s enabled: %-9s active: %s\n' "$unit" \
        "$(systemctl is-enabled "$unit" 2>/dev/null)" "$(systemctl is-active "$unit" 2>/dev/null)"
    done
  fi
  if daemon_up; then ok "DeepSeek 7B daemon running (test_llm)"; else note "DeepSeek 7B daemon not running"; fi
  if serving; then
    ok "Qwen3.5-4B answers at $API (pid $(vllm_pid || echo '?'))"
    if do_stale; then
      note "vLLM runs on other arguments than this script's (started by an older one?): serve_qwen35.sh restart"
    fi
  elif vllm_pid >/dev/null; then
    note "Qwen3.5-4B is starting (pid $(vllm_pid)); tail -f $LOG"
  else
    note "Qwen3.5-4B not running"
  fi
  [[ -f "$RUN/llm_was_up" ]] && note "the 7B daemon was stopped for Qwen3.5-4B; stop gives it back"
  worker_up && note "a meeting worker is up ($UPLOADER)"
  return 0
}

case "${1:-}" in
  start) do_start ;;
  stop) do_stop ;;
  restart) do_restart ;;
  status) do_status ;;
  test) do_test ;;
  test-tools) do_test_tools ;;
  which) do_which ;;
  back) do_back "${2:-}" ;;
  exec) do_exec ;;
  ready) do_ready "${2:-}" ;;
  stale) do_stale ;;
  -h|--help|"") sed -n '6,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; [[ -n "${1:-}" ]] ;;
  *) echo "unknown command: $1 (start, stop, restart, status, test, test-tools, which, back)" >&2; exit 2 ;;
esac
