#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

MODULE_NAME_TTS = "tts"

LOG_CATEGORY_VENDOR = "vendor"
LOG_CATEGORY_KEY_POINT = "key_point"

# tts_d prints this once OpenVoice is resident in VP memory.
READY_TOKEN = "READY tts"

# 22050 is compiled into tts_d's SF_INFO (movz x6, #22050) alongside
# channels=1 and SF_FORMAT_WAV|SF_FORMAT_PCM_16. None of it is configurable.
NATIVE_SAMPLE_RATE = 22050

# The transport is 16 kHz G.722 both ways, and the RTSA SDK is told its PCM
# rate once at init rather than per frame -- so the conversion happens here.
OUTPUT_SAMPLE_RATE = 16000

# 20 ms of 16 kHz PCM16 mono.
CHUNK_BYTES = OUTPUT_SAMPLE_RATE * 2 * 20 // 1000

# Sentence-ending punctuation used to split one over-long sentence.
SENTENCE_MARKS = "。！？；.!?;,，"
