#!/usr/bin/env bash
#
# Diagnose the on-board Ambarella LLM demo server and the TEN stack around it.
#
# Read-only: this script changes nothing. It answers three questions --
#   1. is the TEN Go API server running, and did it lose port 8080 to the LLM?
#   2. is the board's LLM backend fully up?
#   3. does the LLM server answer, and in what framing?
#
# Deliberately NOT using `set -e`: a failing probe is a result, not a reason to
# stop. Deliberately not piping unbounded output into `head` either -- that
# kills the producer with SIGPIPE under `pipefail`.

set -uo pipefail

LOG="/tmp/ambarella_llm_diag_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

# Derived from the script's own location, not $HOME: a checkout is not always
# at $HOME/ten-framework, and hardcoding it made a sibling script install into
# a different checkout than the caller was in -- silently, because both existed.
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
URL="${AMBARELLA_LLM_BASE_URL:-http://127.0.0.1:8080}"
MODEL_TYPE="${MODEL_TYPE:-9}"
TIMEOUT="${TIMEOUT:-90}"
SETTLE="${SETTLE:-8}"   # seconds to let the board finish the previous turn
# Every probe shares one Session-Id on purpose. The board counts a distinct id
# as a distinct user, allows exactly one (--max_user 1), and holds a used
# session for 180s before freeing it -- so a per-probe id refuses every probe
# after the first with "current user num (2) > max_user_num (1)". Reset-En is
# 1 on each probe, so sharing the id does not let history leak between them.
SESSION_ID="${SESSION_ID:-1234}"

sec() { printf '\n\033[1m===== %s\033[0m\n' "$*"; }
kv()  { printf '  %-34s %s\n' "$1" "$2"; }

# Collected for the verdict at the end.
GO_PORT=""; LLM_OWNER=""; CLIENT_PROC=0; DEVICE_ENABLE=0

# test_llm writes its log into the directory it was started from, so the path
# is not fixed. Assuming /tmp/log.txt made this script report a model as
# missing when it had only looked in the wrong place. Ask the running process
# which files it has open, and fall back to its working directory.
# Match the process NAME, not the whole command line: -f would also match
# any shell whose arguments merely mention test_llm -- an editor, a grep, or
# this script being written -- and report it as the daemon.
find_llm_log() {
  local pid target cwd
  for pid in $(pgrep -x 'test_llm|test_llm_client' 2>/dev/null); do
    for target in $(ls -l "/proc/$pid/fd" 2>/dev/null | sed -n 's/.* -> //p'); do
      case "$target" in
        /*log.txt|/*.log) [[ -r "$target" ]] && { echo "$target"; return; } ;;
      esac
    done
  done
  for pid in $(pgrep -x 'test_llm|test_llm_client' 2>/dev/null); do
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null) || continue
    [[ -r "$cwd/log.txt" ]] && { echo "$cwd/log.txt"; return; }
  done
  [[ -r /tmp/log.txt ]] && echo /tmp/log.txt
}
LLM_LOG="${AMBARELLA_LLM_LOG:-$(find_llm_log)}"
declare -A RC BYTES

sec "0. Context"
kv "host"        "$(uname -n) $(uname -m)"
kv "date"        "$(date -Is)"
kv "llm url"     "$URL"
kv "model_type"  "$MODEL_TYPE"
kv "settle between probes" "${SETTLE}s"
kv "session id (shared)"   "$SESSION_ID"
kv "log"         "$LOG"

# --------------------------------------------------------------- 1. ports
sec "1. Listening ports"
if command -v ss >/dev/null 2>&1; then
  ss -lntp 2>/dev/null | awk 'NR==1 || /:(3000|8080|8081|49483)[^0-9]/' \
    | sed 's/[[:space:]]\+$//'
  # tr -d would delete those letters wherever they appear in the name, so
  # capture the quoted process name instead.
  LLM_OWNER=$(ss -lntp 2>/dev/null | sed -n '/:8080[^0-9]/ s/.*users:((\"\([^"]*\)\".*/\1/p' | head -1)
  for p in 8080 8081; do
    if ss -lntp 2>/dev/null | grep -q ":${p}[^0-9].*\"api\""; then GO_PORT="$p"; fi
  done
else
  echo "  ss not available"
fi
echo
kv "port 8080 held by" "${LLM_OWNER:-<nothing>}"
kv "Go API server on"  "${GO_PORT:-<not listening>}"

# --------------------------------------------------------------- 2. processes
sec "2. LLM backend processes"
PS_OUT=$(ps -ef 2>/dev/null | grep -E 'test_llm' | grep -v grep)
if [[ -n "$PS_OUT" ]]; then
  echo "$PS_OUT" | sed 's/^/  /'
  echo "$PS_OUT" | grep -q 'test_llm_client' && CLIENT_PROC=1
else
  echo "  (no test_llm process found)"
fi
echo
kv "test_llm_client present" "$([[ $CLIENT_PROC -eq 1 ]] && echo yes || echo '**NO**')"
echo "  The developer kit guide shows two processes when healthy:"
echo "    test_llm -d 1"
echo "    test_llm_client --ip 127.0.0.1 --port 9005 -v 0 -m <model> --model-type N ..."

# --------------------------------------------------------------- 3. model ready
sec "3. Model readiness"
kv "log" "${LLM_LOG:-<not found>}"
if [[ -n "$LLM_LOG" && -r "$LLM_LOG" ]]; then
  MATCHES=$(grep -c 'Device ENABLE' "$LLM_LOG" 2>/dev/null || echo 0)
  kv "'Device ENABLE' lines" "$MATCHES"
  if [[ "$MATCHES" -gt 0 ]]; then DEVICE_ENABLE=1; else DEVICE_ENABLE=0; fi
  grep 'Device ENABLE' "$LLM_LOG" 2>/dev/null | tail -2 | sed 's/^/  /'
  echo "  --- last 5 lines ---"
  tail -5 "$LLM_LOG" | sed 's/^/  /'
else
  # Unknown is not the same as absent, and reporting it as absent sends the
  # reader looking for a model that may be loaded and fine.
  DEVICE_ENABLE=-1
  echo "  no readable log; set AMBARELLA_LLM_LOG to point at it"
fi

# --------------------------------------------------------------- 4. ten env
sec "4. TEN configuration"
if [[ -r "$REPO/ai_agents/.env" ]]; then
  grep -nE '^SERVER_PORT=|^AGENT_SERVER_URL=|^AMBARELLA_LLM_BASE_URL=' \
    "$REPO/ai_agents/.env" | sed 's/^/  /' || echo "  (none of the three set)"
else
  echo "  no $REPO/ai_agents/.env"
fi
for p in 8080 8081; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$p/health" 2>/dev/null)
  kv "GET :$p/health" "${code:-<no response>}"
done

# --------------------------------------------------------------- 5. probes
# One variable changes per probe, so whichever one first fails names the cause.
probe() {
  local tag="$1" desc="$2"; shift 2
  local out="/tmp/ambarella_probe_${tag}.bin"
  printf '\n\033[1m--- %s: %s\033[0m\n' "$tag" "$desc"

  # run_llm_demo.sh is documented with --max_user 1, so the board serves one
  # turn at a time. Without a pause a probe can fail merely because the
  # previous one is still generating, which looks identical to the request
  # itself being refused.
  sleep "$SETTLE"

  local before=0
  [[ -n "$LLM_LOG" && -r "$LLM_LOG" ]] && before=$(wc -l < "$LLM_LOG")

  curl -sS -X POST --url "$URL/" --max-time "$TIMEOUT" --output "$out" "$@"
  local rc=$?

  if [[ -n "$LLM_LOG" && -r "$LLM_LOG" ]]; then
    local after; after=$(wc -l < "$LLM_LOG")
    if [[ "$after" -gt "$before" ]]; then
      echo "  --- log lines added by this request ---"
      tail -n "$((after - before))" "$LLM_LOG" | sed 's/^/    /'
    else
      echo "  (the server logged nothing for this request)"
    fi
  fi
  local n=0; [[ -f "$out" ]] && n=$(wc -c < "$out")
  RC[$tag]=$rc; BYTES[$tag]=$n
  kv "curl exit"     "$rc$([[ $rc -eq 52 ]] && echo '  (empty reply -- server closed without responding)')"
  kv "bytes received" "$n"
  if [[ "$n" -gt 0 ]]; then
    # Analyse rather than dump. The previous version printed 240 octal bytes,
    # which is not enough to reach the </think> delimiter in a typical reply
    # and invites a conclusion drawn from a truncated window.
    python3 - "$out" <<'PYEOF'
import sys, pathlib
raw = pathlib.Path(sys.argv[1]).read_bytes()
text = raw.decode("utf-8", errors="replace")
stripped = text.lstrip()
print("    framing:        %s" % (
    "SSE (starts with 'data:')" if stripped.startswith("data:") else "plain text"))
print("    total bytes:    %d" % len(raw))
idx = text.find("</think>")
if idx < 0:
    print("    </think>:       absent -- the whole body is the answer")
    body = text
else:
    print("    </think>:       at byte offset %d" % idx)
    print("    reasoning:      %d chars before it" % len(text[:idx].strip()))
    body = text[idx + len("</think>"):]
    print("    opening <think>: %s" % ("present" if "<think>" in text[:idx] else
                                       "ABSENT -- generation starts inside the block"))
print("    --- answer (after </think> if present) ---")
ans = body.strip()
print("    " + (ans[:300] if ans else "<empty>"))
if len(ans) > 300:
    print("    ... %d more chars" % (len(ans) - 300))
PYEOF
  fi
}

sec "5. LLM server probes"
echo "Each probe changes exactly one thing from the one above it."

# B1 -- the developer kit guide's own example, verbatim. ASCII body, no
# Content-Type of our own (curl's --data supplies form-urlencoded).
#
# Session-Id must be a DECIMAL INTEGER. The server parses it numerically and
# rejects a zero: a non-numeric value logs "session_id=0 should not be 0" in
# the LLM's log and the connection closes with no HTTP response at all, which
# curl reports as (52) Empty reply from server.
probe B1 "guide example verbatim (ASCII body, non-streaming)" \
  -H "Session-Id: $SESSION_ID" -H "Model-Type: $MODEL_TYPE" \
  -H "Stream-Off: 1" -H "Reset-En: 1" \
  --data "Hello"

# B2 -- same, but a multibyte body.
probe B2 "+ multibyte (UTF-8) body" \
  -H "Session-Id: $SESSION_ID" -H "Model-Type: $MODEL_TYPE" \
  -H "Stream-Off: 1" -H "Reset-En: 1" \
  --data "你好"

# B3 -- same, plus the Content-Type the extension sets (ambarella.py:123).
probe B3 "+ Content-Type: text/plain; charset=utf-8 (what the extension sends)" \
  -H "Session-Id: $SESSION_ID" -H "Model-Type: $MODEL_TYPE" \
  -H "Stream-Off: 1" -H "Reset-En: 1" \
  -H "Content-Type: text/plain; charset=utf-8" \
  --data "你好"

# B4 -- streaming, which is what the extension actually uses by default
# (ambarella.py:66, streaming=True). This is the one that reveals the framing
# that `response_format: auto` has to sniff.
probe B4 "streaming (Stream-Off: 0) -- the extension's real request" \
  -H "Session-Id: $SESSION_ID" -H "Model-Type: $MODEL_TYPE" \
  -H "Stream-Off: 0" -H "Reset-En: 1" \
  -H "Content-Type: text/plain; charset=utf-8" \
  --data "你好，一句話介紹自己"

# B5 -- B1 again. This is the control. If B1 passed and B5 passes too, the
# board is not stuck and B2..B4 failed on their own merits. If B5 also fails,
# everything after B1 failed because the board was still busy or the session
# slot was held, and the ladder proves nothing about the body or the headers.
probe B5 "control: B1 repeated verbatim (ASCII body, non-streaming)" \
  -H "Session-Id: $SESSION_ID" -H "Model-Type: $MODEL_TYPE" \
  -H "Stream-Off: 1" -H "Reset-En: 1" \
  --data "Hello"

# --------------------------------------------------------------- 6. verdict
sec "6. Verdict"
kv "Go API server"        "$([[ -n "$GO_PORT" ]] && echo "listening on $GO_PORT" || echo '**NOT RUNNING** -- /start will fail')"
kv "port 8080 held by"    "${LLM_OWNER:-<nothing>}"
kv "test_llm_client"      "$([[ $CLIENT_PROC -eq 1 ]] && echo present || echo '**MISSING**')"
case "$DEVICE_ENABLE" in
  1)  kv "Device ENABLE seen" "yes" ;;
  0)  kv "Device ENABLE seen" "**NO** -- model may still be loading" ;;
  *)  kv "Device ENABLE seen" "unknown -- no log was found to read" ;;
esac
echo
printf '  %-6s %-8s %-8s %s\n' probe exit bytes meaning
for t in B1 B2 B3 B4 B5; do
  printf '  %-6s %-8s %-8s ' "$t" "${RC[$t]:-?}" "${BYTES[$t]:-0}"
  if [[ "${RC[$t]:-1}" -eq 0 && "${BYTES[$t]:-0}" -gt 0 ]]; then echo "ok"; else echo "FAILED"; fi
done
echo
echo "  READ B5 FIRST -- it repeats B1 exactly."
echo "    B5 ok      -> the board is not stuck; B2..B4 failed on their own merits."
echo "    B5 FAILED  -> the board was still busy or the session was held, and the"
echo "                  ladder below proves nothing. If the log says"
echo "                  \"current user num (2) > max_user_num (1)\", a session is"
echo "                  still open; it is freed 180s after its last use."
echo "                  Re-run with SETTLE=30 $0"
echo
echo "  If EVERY probe failed with curl exit 8, curl is refusing to parse the
  response rather than the server refusing to answer. curl will not show you
  a response it rejects; this will:

    python3 tools/ambarella/probe_llm_wire.py

  Where the ladder first fails names the cause:"
echo "    B1 -> the service itself, unrelated to TEN. Check test_llm_client above,"
echo "          and grep the log above for the reason the request was refused."
echo "    B2 -> multibyte body handling."
echo "    B3 -> the Content-Type header at ambarella.py:123."
echo "    B4 -> streaming specifically; non-streaming would still work."
echo
echo "  If B4 returned bytes, its 'framing:' line above settles response_format:"
echo "    SSE         -> pin response_format to \"sse\""
echo "    plain text  -> pin response_format to \"raw\""
echo
echo "  The </think> lines say whether the board emits chain-of-thought, and"
echo "  whether it opens the block or starts already inside it. That decides"
echo "  how the extension has to strip reasoning before it reaches TTS."
echo
kv "full log" "$LOG"
