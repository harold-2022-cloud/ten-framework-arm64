#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

MODULE_NAME_ASR = "asr"

# ten_ai_base exports these too, but const.py must stay importable without it
# so that config and daemon tests can run outside the container.
LOG_CATEGORY_VENDOR = "vendor"
LOG_CATEGORY_KEY_POINT = "key_point"

# asr_d prints this once the model is resident in VP memory.
READY_TOKEN = "READY asr"

# The daemon's answer to silence. A normal outcome, not an error.
NO_SPEECH_ERR = "ERR no speech."

# 16000 is compiled into asr_d (movz w3, #16000); it is not configurable.
SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2

# asr_d accepts at most 30 seconds of audio per INFER.
MAX_BUFFER_BYTES = BYTES_PER_SECOND * 30
