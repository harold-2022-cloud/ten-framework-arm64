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

ask() {
  local tag="$1" prompt="$2"
  local out="/tmp/ambarella_reason_${tag}.bin"
  printf '\n\033[1m--- %s\033[0m\n' "$tag"
  printf '  prompt: %s\n' "$prompt"
  sleep "$SETTLE"
  curl -sS -X POST --url "$URL/" --max-time "$TIMEOUT" --output "$out" \
    -H "Session-Id: $SESSION_ID" -H "Model-Type: $MODEL_TYPE" \
    -H "Stream-Off: 0" -H "Reset-En: 1" \
    -H "Content-Type: text/plain; charset=utf-8" \
    --data "$prompt"
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "  curl exit $rc -- no reply"
    return
  fi
  python3 - "$out" <<'PYEOF'
import sys, pathlib
raw = pathlib.Path(sys.argv[1]).read_bytes().decode("utf-8", errors="replace")

# Reassemble the SSE stream. The board sends one character per event and
# escapes whitespace, so the literal text never appears contiguously in the
# file -- grepping the raw bytes for a tag finds nothing even when it is there.
events = [ln[len("data:"):].lstrip(" ")
          for ln in raw.split("\n") if ln.startswith("data:")]
text = "".join(events).replace("<SP>", " ").replace("<NL>", "\n")
done = text.endswith("<DONE>")
text = text[:-len("<DONE>")] if done else text

print("    events:        %d" % len(events))
print("    complete:      %s" % ("yes (<DONE>)" if done else "NO -- truncated"))
print("    chars:         %d" % len(text))
if "</think>" in text:
    head, _, tail = text.partition("</think>")
    head, tail = head.strip(), tail.strip()
    print("    </think>:      present")
    print("    reasoning:     %d chars" % len(head))
    print("    answer:        %d chars" % len(tail))
    print("    answer == reasoning: %s" % (head == tail))
    print("    answer text:   %s" % (tail[:160] if tail else "<EMPTY>"))
else:
    print("    </think>:      ABSENT")
    print("    whole text:    %s" % text.strip()[:160])
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
echo
printf '  full log: %s\n' "$LOG"
