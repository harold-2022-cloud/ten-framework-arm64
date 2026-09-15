# sherpa_onnx_asr_python: transcription on the board's CPU

The second half of moving speech off the Ambarella N1-655's Vector Processor.
`sherpa_onnx_tts_python` took synthesis; this takes recognition.

## Why

The board's ASR, TTS and LLM share one Vector Processor and it does not run
them in parallel. Measured on 2026-09-14, ASR under a generating LLM went from
348 ms to 24 s. Ambarella's recommendation is to move speech to the CPU.

Measured on the board on 2026-09-15: sherpa-onnx transcribes at a real-time
factor of 0.2 on one thread. Twenty milliseconds of audio costs about four
milliseconds of CPU.

With this in place the graph needs no cloud speech service: ASR and TTS on the
CPU, the LLM on the VP, and nothing queues behind anything else.

## Engine

`sherpa_onnx.OnlineRecognizer.from_transducer` — streaming, unlike
`ambarella_asr_python`, whose daemon has no partial results and so could only
transcribe on demand.

| | |
| --- | --- |
| Model | `sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20` |
| Languages | Mandarin and English, in one model |
| Rate | 16000 Hz, which is what RTC delivers |
| Fetched by | `ai_agents/agents/scripts/setup_cpu_asr_tts_arm64.sh` into `~/zipformer_asr/models/` |

The model is bilingual, so the single-language limit that constrains the TTS
voice does not apply here. A reply can be understood in either language even
while the Mandarin voice can only speak one.

## What the base class already does

`AsyncASRBaseExtension` carries the protocol: audio frames in, `asr_result`
out, buffering while the model loads, reconnection, the metrics. Seven methods
are ours: `vendor`, `start_connection`, `is_connected`, `stop_connection`,
`input_audio_sample_rate`, `send_audio`, `finalize`.

"Connection" here is a loaded model rather than a socket, exactly as the TTS
side's "HTTP client" is a function call.

## Behaviour

### Configuration

1. The model directory must exist and hold the encoder, decoder, joiner and
   tokens. Missing, the extension fails at start rather than at the first
   spoken word.
2. Thread count is configurable, defaulting to 1. Those cores also carry the
   LLM server, the TEN runtime, the Go server and now the TTS voice.
3. Endpoint detection is configurable through the three silence rules the
   engine exposes, defaulting to the engine's own values.

### Recognition

4. PCM16 from the frame becomes float32 in [-1, 1], which is what
   `accept_waveform` takes.
5. **Decoding must not run on the event loop.** `decode_stream` is a
   synchronous C++ call; it runs in a worker thread.
6. A partial result is emitted when the text changes, never when it repeats.
   The last run emitted the same partial eight times, and every repeat drove
   an interrupt downstream.
7. An endpoint emits a final result and resets the stream, so the next
   utterance starts clean.
8. `finalize()` drains: input is closed, what remains is decoded, and a final
   result is emitted even if no endpoint was detected.

### Lifecycle

9. The model loads once, off the event loop, and is released on stop.
10. Loading is warmed in the background so the first utterance does not pay
    for it.

## Timing

`start_ms` and `duration_ms` come from how much audio has been accepted, not
from the wall clock: the buffer can run ahead of or behind real time while the
model loads, and a wall-clock timestamp would drift against the transcript.

## Not in scope

- Hotwords, language models, and the alternative decoding methods the engine
  offers. Greedy search is what was measured.
- Speaker diarisation and word-level timestamps. `ASRResult.words` stays empty,
  as in the precedent.
- Replacing `ambarella_asr_python`. It stays for anyone who wants the VP path.

## Testing

Unit tests cover what needs no model: PCM conversion, that a repeated partial
is not re-emitted, that an endpoint produces a final and resets, that
`finalize` emits without an endpoint, and that decoding is not on the event
loop. The engine is faked to its real contract — `create_stream`,
`accept_waveform`, `is_ready`, `decode_stream`, `get_result`, `is_endpoint`,
`reset` — so a pass means the adapter holds up its end.

Recognition itself is exercised on the board, where the model is. As with the
TTS side, the suite runs under the interpreter the runtime embeds, not
whichever python is on PATH.
