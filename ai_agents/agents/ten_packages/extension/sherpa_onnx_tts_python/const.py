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
