# sherpa_onnx_asr_python

Streaming speech recognition on the CPU of an Ambarella N1-655, through a
Zipformer transducer run by
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

The recognition half of moving speech off the board's Vector Processor;
`sherpa_onnx_tts_python` is the synthesis half. Neither opens `/dev/cavalry`.

## Why it exists

The board's ASR, TTS and LLM share one Vector Processor and it does not run
them in parallel. Measured on 2026-09-14, ASR under a generating LLM went from
348 ms to 24 s. Ambarella's recommendation is to move speech to the CPU.

## Model

`sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20` — **Mandarin and
English in one model**, so the single-language limit that constrains the Piper
voice does not reach the transcript.

Fetched by:

```bash
ai_agents/agents/scripts/setup_cpu_asr_tts_arm64.sh
```

into `~/zipformer_asr/models/`, which is what `property.json` points at.

Measured on the board on 2026-09-15: a real-time factor of 0.2 on one thread.
Twenty milliseconds of audio costs about four milliseconds of CPU.

## Properties

| Property | Default | |
| --- | --- | --- |
| `model_dir` | — | The bundle directory. Required; refused at start if absent. |
| `num_threads` | 1 | Those cores also carry the LLM server, the runtime, the Go server and the TTS voice. |
| `enable_endpoint_detection` | true | The engine decides where an utterance ends. |
| `rule1_min_trailing_silence` | 2.4 | Silence that ends an utterance. |
| `rule2_min_trailing_silence` | 1.2 | Silence after a word that ends it. |
| `rule3_min_utterance_length` | 20.0 | Length that ends it regardless. |
| `language` | `zh-CN` | Reported on the result. Not an instruction to the model, which is bilingual. |

## Two things this has to get right

**Decoding does not run on the event loop.** `decode_stream` is a synchronous
C++ call. It runs in a worker thread, as the TTS side's `generate` does.

**A partial is emitted only when it changes.** The engine reports the text so
far after every step, so an unchanged transcript arrives many times a second.
On the board's first run the same partial went out eight times and every repeat
drove an interrupt downstream.

## Installing the dependencies

The runtime embeds its own Python — 3.12 on this board, while the shell's is
3.13 — and its `sys.path` has no user-site directory. Installing the obvious
way puts the packages where the runtime will never look:

```bash
tools/ambarella/setup_tts_runtime_deps.sh
```

reads the interpreter out of `TEN_PYTHON_LIB_PATH`, installs for that one, and
verifies by importing as it.

## Tests

```bash
task test-extension EXTENSION=agents/ten_packages/extension/sherpa_onnx_asr_python
```

The engine is faked to its real contract — `create_stream`,
`accept_waveform`, `is_ready`, `decode_stream`, `get_result`, `is_endpoint`,
`reset` — so the 300 MB model is not needed. Recognition itself is exercised on
the board. The harness runs under the interpreter the runtime embeds, and says
so; a pass under a different one proves nothing about the runtime.
