#!/usr/bin/env bash
#
# Measure how the board's LLM frames its replies, so the extension can stop
# guessing how to separate reasoning from the answer.
#
# Answers four questions with evidence rather than argument:
#   1. which models are actually installed on this board
#   2. does every complete reply carry a </think> delimiter
#   3. how much of a reply is reasoning, and does the answer merely repeat it
#   4. can a prompt suppress the reasoning altogether
#
# Read-only. One Session-Id throughout: the board counts a distinct id as a
# distinct user, allows one, and holds a used session for 180s.

set -uo pipefail

LOG="/tmp/ambarella_reasoning_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

URL="${AMBARELLA_LLM_BASE_URL:-http://127.0.0.1:8080}"
MODEL_TYPE="${MODEL_TYPE:-9}"
SESSION_ID="${SESSION_ID:-1234}"
SETTLE="${SETTLE:-8}"
TIMEOUT="${TIMEOUT:-120}"

sec() { printf '\n\033[1m===== %s\033[0m\n' "$*"; }

sec "0. Context"
printf '  %-20s %s\n' url "$URL" model_type "$MODEL_TYPE" \
                      session "$SESSION_ID" settle "${SETTLE}s" log "$LOG"

sec "1. Models installed on this board"
# run_llm_demo.sh picks the model by the folder name under --model_path.
for d in ~/demo_resources/llm_demo/*/; do
  [[ -d "$d" ]] && printf '  %s\n' "$(basename "$d")"
done
echo "  (model_type values: 0 chatllama_13B, 1 codellama_13B, 2 gemma_7B,"
echo "   3 llama3_8B, 4 chatllama_7B, 5 gemma2_2B, 6 phi3_3.8B_128K,"
echo "   7 qwen_7B, 8 qwen2_7B, 9 deepseek_7B, 10 deepseek_32B,"
echo "   11 tinyllama_1.1B, 12 gemma_2B, 13 phi3_3.8B_4K, 14 qwen2_0.5B,"
echo "   15 qwen2_1.5B, 16 deepseek_1.5B, 17 llama3.1_8B)"
echo "  Only deepseek_* reasons. Any other installed model sidesteps the"
echo "  reasoning problem entirely."

sec "2. Reply framing, one prompt at a time"

# The cost of buffering until </think> is a wall-clock number, not an opinion,
# so the request is made from Python and every chunk is timestamped as it
# arrives. curl can report time-to-first-byte but not time-to-a-token.
ask() {
  local tag="$1" prompt="$2"
  printf '\n\033[1m--- %s\033[0m\n' "$tag"
  printf '  prompt: %s\n' "$prompt"
  sleep "$SETTLE"
  AMB_URL="$URL/" AMB_SESSION="$SESSION_ID" AMB_MODEL="$MODEL_TYPE" \
  AMB_TIMEOUT="$TIMEOUT" AMB_PROMPT="$prompt" AMB_OUT="/tmp/ambarella_reason_${tag}.bin" \
  python3 - <<'PYEOF'
import os, time, urllib.request, pathlib

url, out = os.environ["AMB_URL"], pathlib.Path(os.environ["AMB_OUT"])
req = urllib.request.Request(
    url, data=os.environ["AMB_PROMPT"].encode("utf-8"), method="POST",
    headers={
        "Session-Id": os.environ["AMB_SESSION"],
        "Model-Type": os.environ["AMB_MODEL"],
        "Stream-Off": "0",
        "Reset-En": "1",
        "Content-Type": "text/plain; charset=utf-8",
    })

t0 = time.monotonic()
t_first = t_think = t_done = None
buf = b""
raw_text = ""           # events joined, still escaped
pending = ""            # partial SSE line carried across chunks

# Decoding runs on the joined text, never per event: an escape can be split
# across events exactly as </think> is, and a per-event replace would miss it.
def decode(t):
    return t.replace("<SP>", " ").replace("<NL>", "\n")

try:
    with urllib.request.urlopen(req, timeout=float(os.environ["AMB_TIMEOUT"])) as r:
        while True:
            chunk = r.read(64)
            if not chunk:
                break
            now = time.monotonic()
            if t_first is None:
                t_first = now
            buf += chunk
            pending += chunk.decode("utf-8", errors="replace")
            *lines, pending = pending.split("\n")
            for ln in lines:
                if ln.startswith("data:"):
                    raw_text += ln[5:].lstrip(" ")
            if t_think is None and "</think>" in raw_text:
                t_think = now
            if t_done is None and "<DONE>" in raw_text:
                t_done = now
                break
except Exception as e:                      # noqa: BLE001 - report, do not raise
    print("    request failed: %s: %s" % (type(e).__name__, e))
    raise SystemExit(0)

out.write_bytes(buf)
t_end = time.monotonic()
if pending.startswith("data:"):
    raw_text += pending[5:].lstrip(" ")

done = "<DONE>" in raw_text
text = decode(raw_text.replace("<DONE>", ""))
el = lambda t: "n/a" if t is None else "%.2fs" % (t - t0)

print("    complete:        %s" % ("yes (<DONE>)" if done else "NO -- truncated"))
print("    chars:           %d" % len(text))
print("    first byte at:   %s" % el(t_first))
print("    total:           %s" % el(t_end))
if "</think>" in text:
    head, _, tail = text.partition("</think>")
    head, tail = head.strip(), tail.strip()
    print("    </think>:        present, at %s" % el(t_think))
    print("    reasoning:       %d chars" % len(head))
    print("    answer:          %d chars" % len(tail))
    print("    answer == reasoning: %s" % (head == tail))
    print("    >> BUFFERING COST: %s before the answer could start" % el(t_think))
    print("    answer text:     %s" % (tail[:160] if tail else "<EMPTY>"))
else:
    print("    </think>:        ABSENT")
    print("    >> BUFFERING COST: %s -- nothing could be released until the end"
          % el(t_end))
    print("    whole text:      %s" % text.strip()[:160])
PYEOF
}

ask A "Hello"
ask B "你好"
ask C "What is 2+2? Answer with just the number."
ask D "介紹一下你自己，一句話就好"
# Can the reasoning be suppressed by instruction alone?
ask E "Answer directly with no reasoning and no preamble. Question: what is the capital of France?"
ask F "/no_think What is the capital of France?"

sec "3. What to read"
echo "  complete:  a reply without <DONE> was truncated and proves nothing."
echo "  </think>:  if every complete reply has it, buffering until it arrives"
echo "             is bounded and safe. If some replies lack it, buffering has"
echo "             to fall back to releasing everything at <DONE>."
echo "  answer == reasoning:"
echo "             true means the model repeats itself and the reasoning half"
echo "             is pure waste -- dropping it costs nothing."
echo "  E and F:   if either suppresses </think>, the problem is solved by"
echo "             configuration rather than by parsing."
echo "  BUFFERING COST:"
echo "             the wall-clock delay option A would add before the first"
echo "             word reaches TTS. Compare it against 'first byte at' --"
echo "             the delay option B has today."
echo
printf '  full log: %s\n' "$LOG"
