# sherpa_onnx_tts_python

Speech synthesis on the CPU of an Ambarella N1-655, through a Piper VITS voice
run by [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

## Why it exists

The board's three on-device models — ASR, TTS and the LLM — share one Vector
Processor, and it does not run them in parallel. Measured on 2026-09-14, ASR
under a generating LLM went from 348 ms to 24 s, and one LLM turn reached 24
minutes. Ambarella's recommendation is to move speech to the CPU, which is
otherwise idle. This extension is the TTS half of that.

Neither it nor its ASR sibling opens `/dev/cavalry`.

## Measured on the board

One thread, voice `vits-piper-zh_CN-huayan-medium`, 2026-09-15:

| | |
| --- | --- |
| Load | 2.0 s |
| Native rate | 22050 Hz |
| Real-time factor | 0.34 – 0.38 |
| First audio | about a fifth of the total, on a multi-sentence reply |

The real-time factor is flat from 3 characters to 57, so synthesis cost is
linear in text length.

## Installing the voice

`pip install sherpa-onnx` — PyPI publishes `manylinux2014_aarch64` wheels, so
the board needs no toolchain. Then fetch a voice:

```bash
ai_agents/agents/scripts/setup_cpu_asr_tts_arm64.sh
```

It unpacks to `~/piper_tts/vits/vits-piper-zh_CN-huayan-medium`, which is what
`property.json` points at. The bundle must come from sherpa-onnx rather than
from Hugging Face directly: `OfflineTtsVitsModelConfig` wants `tokens.txt` and
the espeak-ng data, which the raw `.onnx` + `.onnx.json` pair does not carry.

To hear a voice before wiring it in:

```bash
python3 tools/ambarella/probe_piper_tts.py
```

## Properties

| Property | Default | |
| --- | --- | --- |
| `voice_dir` | — | The bundle directory. Required; refused at start if absent. |
| `output_sample_rate` | 16000 | What the RTC chain is declared with. Conversion runs only when the voice differs. |
| `num_threads` | 1 | See below. |
| `speed` | 1.0 | |
| `speaker_id` | 0 | The Mandarin voice has one speaker. |

One thread is the default because two are only 13% faster and four are slower
than one, measured on this board — and those four cores also carry the LLM
server, the TEN runtime and the Go server.

## A contract worth knowing about

`OfflineTts.generate()` documents its callback as "Return a non-zero value to
stop generation early". Measured against sherpa-onnx 1.13.8, the opposite is
true: **zero stops, non-zero continues**. Following the docstring truncates
every reply to its first sentence, silently.

`const.py` names both values and `tests/test_engine_contract.py` pins them
against the installed library. That test needs a voice, so it skips unless one
is named:

```bash
SHERPA_ONNX_TTS_VOICE_DIR=~/piper_tts/vits/vits-piper-zh_CN-huayan-medium \
    ./tests/bin/start -k engine_contract
```

Run it after any sherpa-onnx upgrade.

## Language

A Piper VITS voice speaks one language — the models are trained per language,
which is the architecture rather than an oversight. Mandarin comes first
because that is what the board is being brought up in.

One voice carries both Mandarin and English, `vits-melo-tts-zh_en` (167 MB,
44100 Hz), and because it is also a VITS model it loads through the same
config. Moving to it is a `voice_dir` change, not a code change; its speed on
this board is unmeasured.

The multilingual alternatives that are *not* VITS need more: Matcha
(`matcha-icefall-zh-en`) needs a separate vocoder file and Kokoro
(`kokoro-int8-multi-lang-v1_1`) needs per-language lexicons. Both use a
different config class, so neither is a path change.

## Tests

```bash
task test-extension EXTENSION=agents/ten_packages/extension/sherpa_onnx_tts_python
```

The unit tests fake the engine: they assert against sherpa-onnx's contract — a
`sample_rate` attribute and a `generate()` that calls back per sentence and
honours the return value — without the 67 MB voice. Synthesis itself is heard
on the board.
