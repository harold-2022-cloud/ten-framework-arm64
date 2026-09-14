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

REPO="${REPO:-$HOME/ten-framework}"
URL="${AMBARELLA_LLM_BASE_URL:-http://127.0.0.1:8080}"
MODEL_TYPE="${MODEL_TYPE:-9}"
TIMEOUT="${TIMEOUT:-90}"

sec() { printf '\n\033[1m===== %s\033[0m\n' "$*"; }
kv()  { printf '  %-34s %s\n' "$1" "$2"; }

# Collected for the verdict at the end.
GO_PORT=""; LLM_OWNER=""; CLIENT_PROC=0; DEVICE_ENABLE=0
declare -A RC BYTES

sec "0. Context"
kv "host"        "$(uname -n) $(uname -m)"
kv "date"        "$(date -Is)"
kv "llm url"     "$URL"
kv "model_type"  "$MODEL_TYPE"
kv "log"         "$LOG"

# --------------------------------------------------------------- 1. ports
sec "1. Listening ports"
if command -v ss >/dev/null 2>&1; then
  ss -lntp 2>/dev/null | awk 'NR==1 || /:(3000|8080|8081|49483)\y/' \
    | sed 's/[[:space:]]\+$//'
  # tr -d would delete those letters wherever they appear in the name, so
  # capture the quoted process name instead.
  LLM_OWNER=$(ss -lntp 2>/dev/null | sed -n '/:8080[^0-9]/ s/.*users:((\"\([^"]*\)\".*/\1/p' | head -1)
  for p in 8080 8081; do
    if ss -lntp 2>/dev/null | grep -q ":${p}\y.*\"api\""; then GO_PORT="$p"; fi
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
sec "3. Model readiness (/tmp/log.txt)"
if [[ -r /tmp/log.txt ]]; then
  MATCHES=$(grep -c 'Device ENABLE' /tmp/log.txt 2>/dev/null || echo 0)
  kv "'Device ENABLE' lines" "$MATCHES"
  [[ "$MATCHES" -gt 0 ]] && DEVICE_ENABLE=1
  grep 'Device ENABLE' /tmp/log.txt 2>/dev/null | tail -2 | sed 's/^/  /'
  echo "  --- last 5 lines of /tmp/log.txt ---"
  tail -5 /tmp/log.txt | sed 's/^/  /'
else
  echo "  /tmp/log.txt not readable"
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
  curl -sS -X POST --url "$URL/" --max-time "$TIMEOUT" --output "$out" "$@"
  local rc=$?
  local n=0; [[ -f "$out" ]] && n=$(wc -c < "$out")
  RC[$tag]=$rc; BYTES[$tag]=$n
  kv "curl exit"     "$rc$([[ $rc -eq 52 ]] && echo '  (empty reply -- server closed without responding)')"
  kv "bytes received" "$n"
  if [[ "$n" -gt 0 ]]; then
    echo "  --- first 240 bytes, escaped (shows framing: 'data:' prefix = SSE) ---"
    od -c -N 240 "$out" | sed 's/^/  /'
    echo "  --- as text ---"
    dd if="$out" bs=1 count=400 2>/dev/null | sed 's/^/  /'
    echo
  fi
}

sec "5. LLM server probes"
echo "Each probe changes exactly one thing from the one above it."

# B1 -- the developer kit guide's own example, verbatim. ASCII body, no
# Content-Type of our own (curl's --data supplies form-urlencoded).
probe B1 "guide example verbatim (ASCII body, non-streaming)" \
  -H "Session-Id: diag1" -H "Model-Type: $MODEL_TYPE" \
  -H "Stream-Off: 1" -H "Reset-En: 1" \
  --data "Hello"

# B2 -- same, but a multibyte body.
probe B2 "+ multibyte (UTF-8) body" \
  -H "Session-Id: diag2" -H "Model-Type: $MODEL_TYPE" \
  -H "Stream-Off: 1" -H "Reset-En: 1" \
  --data "你好"

# B3 -- same, plus the Content-Type the extension sets (ambarella.py:123).
probe B3 "+ Content-Type: text/plain; charset=utf-8 (what the extension sends)" \
  -H "Session-Id: diag3" -H "Model-Type: $MODEL_TYPE" \
  -H "Stream-Off: 1" -H "Reset-En: 1" \
  -H "Content-Type: text/plain; charset=utf-8" \
  --data "你好"

# B4 -- streaming, which is what the extension actually uses by default
# (ambarella.py:66, streaming=True). This is the one that reveals the framing
# that `response_format: auto` has to sniff.
probe B4 "streaming (Stream-Off: 0) -- the extension's real request" \
  -H "Session-Id: diag4" -H "Model-Type: $MODEL_TYPE" \
  -H "Stream-Off: 0" -H "Reset-En: 1" \
  -H "Content-Type: text/plain; charset=utf-8" \
  --data "你好，一句話介紹自己"

# --------------------------------------------------------------- 6. verdict
sec "6. Verdict"
kv "Go API server"        "$([[ -n "$GO_PORT" ]] && echo "listening on $GO_PORT" || echo '**NOT RUNNING** -- /start will fail')"
kv "port 8080 held by"    "${LLM_OWNER:-<nothing>}"
kv "test_llm_client"      "$([[ $CLIENT_PROC -eq 1 ]] && echo present || echo '**MISSING**')"
kv "Device ENABLE seen"   "$([[ $DEVICE_ENABLE -eq 1 ]] && echo yes || echo '**NO** -- model may still be loading')"
echo
printf '  %-6s %-8s %-8s %s\n' probe exit bytes meaning
for t in B1 B2 B3 B4; do
  printf '  %-6s %-8s %-8s ' "$t" "${RC[$t]:-?}" "${BYTES[$t]:-0}"
  if [[ "${RC[$t]:-1}" -eq 0 && "${BYTES[$t]:-0}" -gt 0 ]]; then echo "ok"; else echo "FAILED"; fi
done
echo
echo "  Where the ladder first fails names the cause:"
echo "    B1 -> the service itself, unrelated to TEN. Check test_llm_client above."
echo "    B2 -> multibyte body handling."
echo "    B3 -> the Content-Type header at ambarella.py:123."
echo "    B4 -> streaming specifically; non-streaming would still work."
echo
echo "  If B4 returned bytes, its od -c dump above settles response_format:"
echo "    starts with 'data:'  -> pin response_format to \"sse\""
echo "    plain text           -> pin response_format to \"raw\""
echo
kv "full log" "$LOG"
