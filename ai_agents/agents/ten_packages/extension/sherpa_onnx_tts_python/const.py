#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

# What the RTC chain is declared with. agora_rtc states its PCM rate once,
# when the service starts, so every frame after that has to arrive at it.
OUTPUT_SAMPLE_RATE = 16000

# Measured on an N1-655: two threads are 13% faster than one and four are
# slower than one. Those cores also carry the LLM server, the TEN runtime and
# the Go server, so one is the default.
DEFAULT_NUM_THREADS = 1

LOG_CATEGORY_KEY_POINT = "key_point"
LOG_CATEGORY_VENDOR = "vendor"

# generate()'s callback steers the synthesis through its return value, and the
# polarity is the reverse of what its docstring states: zero stops, non-zero
# continues. Measured against sherpa-onnx 1.13.8. Named rather than written as
# bare 0 and 1 because the wrong one is silent -- every reply simply ends
# after its first sentence.
CALLBACK_STOP = 0
CALLBACK_CONTINUE = 1

# One yield becomes one AudioFrame, so this is the frame length the pipeline
# sees. 20 ms is what the rest of the chain is built around, and it also
# bounds how much already-synthesised audio a barge-in has to talk over.
FRAME_MS = 20

# sherpa-onnx calls back once per sentence, and it ends a sentence only at
# 。！？ and their western equivalents. A line written with commas is one
# callback however long it is, and the first audio waits for its last word.
#
# Two separate numbers, because they pull opposite ways. Text at or below
# MIN_CHARS_TO_SPLIT is left whole -- waiting for it costs less than the seam
# a split leaves -- so a lower value splits more. CHARS_PER_PIECE packs the
# clauses after the first, where the only job is keeping ahead of playback
# and fewer calls do it better, so a higher value is better there. Running
# both off one number meant raising the pack limit stopped the greeting
# splitting at all: at 24 the deployed 20-character greeting was returned
# whole and its first audio took 3.60 s.
#
# Measured against the engine on that greeting: 8 gives first audio at
# 0.62 s, 12 at 0.47 s, 16 at 0.38 s. Eight is the value here because it is
# also low enough to split a 12-character answer (1.74 s to 0.98 s), which
# the higher ones leave whole.
MIN_CHARS_TO_SPLIT = 8
CHARS_PER_PIECE = 24

# Where a clause may end. The sentence marks are here too, so a long run of
# sentences is also broken up rather than handed over whole.
CLAUSE_MARKS = "，,、；;：:。.！!？?"
