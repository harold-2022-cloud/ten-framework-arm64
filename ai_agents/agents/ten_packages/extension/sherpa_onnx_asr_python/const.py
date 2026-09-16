#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

# What the model was trained at and what RTC delivers, so nothing is
# resampled on this path.
SAMPLE_RATE = 16000

# Measured on an N1-655 for the TTS voice: two threads are 13% faster than one
# and four are slower than one. The same four cores now carry the LLM server,
# the TEN runtime, the Go server and the voice, so one is the default here too.
DEFAULT_NUM_THREADS = 1

MODULE_NAME_ASR = "asr"

LOG_CATEGORY_KEY_POINT = "key_point"
LOG_CATEGORY_VENDOR = "vendor"

# generate_file_name appends a timestamp and the extension. Captured audio is
# named per run rather than with one fixed name because the reason to turn the
# dump on is to compare several sessions, and a fixed name means each run
# destroys the evidence from the last.
DUMP_FILE_PREFIX = "sherpa_onnx_asr_in"
