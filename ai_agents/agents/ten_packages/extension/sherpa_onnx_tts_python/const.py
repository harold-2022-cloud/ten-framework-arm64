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
