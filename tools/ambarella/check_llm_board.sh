#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Compare the board's LLM against what the Ambarella developer kit guide says
# it should be, one item at a time, with the guide's own wording quoted.
#
#   tools/ambarella/check_llm_board.sh
#   tools/ambarella/check_llm_board.sh --no-request   # skip the curl probe
#
# Reads only, apart from the one request the guide itself documents. It starts
# nothing, stops nothing, and installs nothing. Always exits 0: a failed check
# is the output, not an error.
#
# Every "guide says" line below is quoted from the LLM section of the kit's
# own HTML guide. Where reality differs, the difference is the finding --
# there is no inference in this script beyond the comparison.

set -u

GUIDE_LAUNCHER="/usr/share/ambarella/llm_demo/run_llm_demo.sh"
GUIDE_MODEL_PARENT="$HOME/demo_resources/llm_demo"
GUIDE_MODEL_DIR="$GUIDE_MODEL_PARENT/deepseek_7B"
GUIDE_LOG="/tmp/log.txt"
GUIDE_URL="http://127.0.0.1:8080"
GUIDE_LOAD_SECONDS=80

DO_REQUEST=1
for arg in "$@"; do
  case "$arg" in
    --no-request) DO_REQUEST=0 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Match the process NAME, not the whole command line: -f would also match
# any shell whose arguments merely mention test_llm -- an editor, a grep, or
# this script being written -- and report it as the daemon.
llm_pids() { pgrep -x 'test_llm|test_llm_client' 2>/dev/null; }

sec()   { printf '\n\033[1m===== %s\033[0m\n' "$*"; }
guide() { printf '  guide: %s\n' "$*"; }
found() { printf '  board: %s\n' "$*"; }
same()  { printf '  \033[32mMATCHES\033[0m\n'; }
diff_() { printf '  \033[31mDIFFERS\033[0m  %s\n' "$*"; }
note()  { printf '  note:  %s\n' "$*"; }

LOG="/tmp/llm_board_check_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

# ------------------------------------------------------------------ 1
sec "1. The launcher"
guide "cd /usr/share/ambarella/llm_demo/ && ./run_llm_demo.sh --run_mode start \\"
guide "    --model_type 9 --model_path ~/demo_resources/llm_demo --ip 127.0.0.1 --max_user 1"
if [[ -x "$GUIDE_LAUNCHER" ]]; then
  found "$GUIDE_LAUNCHER is present and executable"
  same
else
  found "$GUIDE_LAUNCHER not found"
  diff_ "the documented way to start the demo is not on this board"
fi

# ------------------------------------------------------------------ 2
sec "2. Processes"
guide "a healthy board shows exactly two:"
guide "  test_llm -d 1"
guide "  test_llm_client --ip 127.0.0.1 --port 9005 -v 0 \\"
guide "      -m ~/demo_resources/llm_demo/deepseek_7B --model-type 9 --bsize 64 --user-num 1"
DAEMON=0
CLIENT=0
CLIENT_MODEL=""
while IFS= read -r pid; do
  [[ -n "$pid" ]] || continue
  argv=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
  [[ -n "$argv" ]] || continue
  cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null)
  found "pid $pid  cwd $cwd"
  found "   $argv"
  case "$argv" in
    test_llm_client*)
      CLIENT=1
      CLIENT_MODEL=$(echo "$argv" | sed -n 's/.*-m \([^ ]*\).*/\1/p')
      ;;
    test_llm\ *|test_llm) DAEMON=1 ;;
  esac
done < <(llm_pids)

if [[ $DAEMON -eq 1 && $CLIENT -eq 1 ]]; then
  same
else
  [[ $DAEMON -eq 1 ]] || diff_ "test_llm (the daemon) is not running"
  [[ $CLIENT -eq 1 ]] || diff_ "test_llm_client (loads the model) is not running"
fi

# The guide's own ps output shows both started by the launcher, so their cwd
# is the launcher's. Started by hand from elsewhere, the log lands elsewhere
# too -- which is how /tmp/log.txt came to be missing.
while IFS= read -r pid; do
  [[ -n "$pid" ]] || continue
  cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null)
  if [[ -n "$cwd" && "$cwd" != "/tmp" && "$cwd" != "/usr/share/ambarella/llm_demo" ]]; then
    note "pid $pid runs from $cwd; the guide expects the log at $GUIDE_LOG"
  fi
done < <(llm_pids)

# ------------------------------------------------------------------ 3
sec "3. The model artifacts"
guide "tar xvf ~/demo_resources/models/Deepseek-R1-Distill-Qwen/n1-655_deepseek_r1_distill_qwen_7B_1NVP.tar"
guide "    -C ~/demo_resources/llm_demo/deepseek_7B"
if [[ -d "$GUIDE_MODEL_DIR" ]]; then
  found "$GUIDE_MODEL_DIR exists, $(du -sh "$GUIDE_MODEL_DIR" 2>/dev/null | cut -f1)"
  for required in weights tokenizer.json model_desc.json; do
    if [[ -e "$GUIDE_MODEL_DIR/$required" ]]; then
      found "   $required present"
    else
      diff_ "$required missing from $GUIDE_MODEL_DIR"
    fi
  done
  same
else
  found "$GUIDE_MODEL_DIR does not exist"
  diff_ "the model was never extracted, or went somewhere else"
fi
if [[ -n "$CLIENT_MODEL" ]]; then
  expanded="${CLIENT_MODEL/#\~/$HOME}"
  found "the running client was given -m $CLIENT_MODEL"
  [[ "$expanded" == "$GUIDE_MODEL_DIR" ]] && same ||
    diff_ "the guide's example uses $GUIDE_MODEL_DIR"
fi

# ------------------------------------------------------------------ 4
sec "4. Model readiness"
guide "until the log below can be seen from /tmp/log.txt on the board:"
guide "  [INFO] [01-01 17:33:55] Device ENABLE: fd_dev: 5, net_type: 9, sid: 0"
guide "the first time of loading model may take up to ${GUIDE_LOAD_SECONDS} s after boot"

LOG_PATH=""
if [[ -r "$GUIDE_LOG" ]]; then
  LOG_PATH="$GUIDE_LOG"
else
  # Not where the guide says, so ask the processes where they actually write.
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    while IFS= read -r target; do
      case "$target" in
        /*log.txt|/*.log) [[ -r "$target" ]] && { LOG_PATH="$target"; break 2; } ;;
      esac
    done < <(ls -l "/proc/$pid/fd" 2>/dev/null | sed -n 's/.* -> //p')
  done < <(llm_pids)
fi

if [[ -z "$LOG_PATH" ]]; then
  found "no log found, at $GUIDE_LOG or among the processes' open files"
  diff_ "readiness cannot be judged; this is unknown, not absent"
else
  found "log at $LOG_PATH"
  [[ "$LOG_PATH" == "$GUIDE_LOG" ]] || note "the guide expects $GUIDE_LOG"
  hits=$(grep -c 'Device ENABLE' "$LOG_PATH" 2>/dev/null || echo 0)
  found "'Device ENABLE' lines: $hits"
  if [[ "$hits" -gt 0 ]]; then
    grep 'Device ENABLE' "$LOG_PATH" | tail -2 | sed 's/^/         /'
    same
  else
    diff_ "the model has not reached the device"
  fi
  found "last 8 lines:"
  tail -8 "$LOG_PATH" | sed 's/^/         /'
fi

up=$(cut -d. -f1 /proc/uptime 2>/dev/null)
[[ -n "$up" ]] && found "system up ${up}s$([[ "$up" -lt "$GUIDE_LOAD_SECONDS" ]] && echo "  -- under the ${GUIDE_LOAD_SECONDS}s the guide allows for a first load")"

# ------------------------------------------------------------------ 5
sec "5. The documented conflict"
guide "Do not run this LLM demo and LLaVA VLM demo at the same time, as they"
guide "are both using the same library which cannot support such usage."
CONFLICT=$(pgrep -af 'test_llava|eazyai|vlc' 2>/dev/null | grep -v check_llm_board)
if [[ -n "$CONFLICT" ]]; then
  echo "$CONFLICT" | sed 's/^/  board: /'
  diff_ "a conflicting demo is running; the guide forbids this combination"
else
  found "no test_llava, eazyai or vlc process"
  same
fi

# ------------------------------------------------------------------ 6
sec "6. Ports"
guide "use the http://127.0.0.1:8080 link to connect to the LLM demo"
guide "the client connects to the daemon on port 9005"
if command -v ss >/dev/null 2>&1; then
  ss -lntp 2>/dev/null | awk 'NR==1 || /:(8080|8081|9005|3000|49483)[^0-9]/' | sed 's/^/  board: /'
  holder=$(ss -lntp 2>/dev/null | sed -n '/:8080[^0-9]/ s/.*users:((\"\([^"]*\)\".*/\1/p' | head -1)
  found "port 8080 held by: ${holder:-<nothing>}"
  [[ "$holder" == "test_llm" ]] && same || diff_ "the guide expects test_llm on 8080"
  ss -ntp 2>/dev/null | grep -q ':9005' && found "9005 has an established connection" ||
    diff_ "no established connection on 9005; the client is not attached"
else
  found "ss not available"
fi

# ------------------------------------------------------------------ 7
if [[ $DO_REQUEST -eq 1 ]]; then
  sec "7. The documented request"
  guide 'curl -X POST --url http://127.0.0.1:8080/ -H "Session-Id: 1234" \'
  guide '     -H "Model-Type: 9" -H "Stream-Off: 1" -H "Reset-En: 1" --data "Hello"'
  note "sent over a raw socket as well, because curl refuses to hand over a"
  note "response it will not parse and this board's error page is malformed"

  OUT="/tmp/llm_board_check_response.bin"
  curl -sS -X POST --url "$GUIDE_URL/" --max-time 60 \
    -H "Session-Id: 1234" -H "Model-Type: 9" \
    -H "Stream-Off: 1" -H "Reset-En: 1" \
    --data "Hello" --output "$OUT"
  rc=$?
  found "curl exit $rc, $(wc -c < "$OUT" 2>/dev/null || echo 0) bytes"
  [[ $rc -eq 0 ]] && same || diff_ "the guide's own example does not complete"

  python3 - "$GUIDE_URL" <<'PYEOF'
import socket, sys, urllib.parse
url = urllib.parse.urlparse(sys.argv[1])
body = b"Hello"
req = (
    b"POST / HTTP/1.1\r\n"
    + f"Host: {url.hostname}:{url.port}\r\n".encode()
    + b"Session-Id: 1234\r\nModel-Type: 9\r\nStream-Off: 1\r\nReset-En: 1\r\n"
    + f"Content-Length: {len(body)}\r\n".encode()
    + b"Connection: close\r\n\r\n"
    + body
)
try:
    s = socket.create_connection((url.hostname, url.port), timeout=60)
except OSError as err:
    print(f"  board: raw socket could not connect: {err}")
    raise SystemExit
data = bytearray()
try:
    s.sendall(req)
    while len(data) < 4096:
        chunk = s.recv(8192)
        if not chunk:
            break
        data.extend(chunk)
except socket.timeout:
    pass
finally:
    s.close()
raw = bytes(data)
print(f"  board: raw socket received {len(raw)} bytes")
print(f"  board: {raw[:200]!r}")
split = raw.find(b"\r\n\r\n")
if split < 0:
    print("  board: no header block; the body arrives bare")
    raise SystemExit
head = raw[:split].decode("utf-8", "replace").split("\r\n")
print(f"  board: status line {head[0]!r}")
for line in head[1:]:
    if line and ":" not in line:
        print(f"  board: header {line!r} has no colon -- this is what curl rejects")
if head[0].startswith("HTTP/") and " 404 " in head[0]:
    print("  board: 404 -- the server is up and served no model for this route")
PYEOF
fi

sec "Done"
echo "  full output: $LOG"
exit 0
