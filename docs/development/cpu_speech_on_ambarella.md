# Running speech on the board's CPU

How the Ambarella N1-655 runs speech synthesis without the Vector Processor,
why it has to, and what it measured.

## The problem

The board carries three on-device models — `asr_d` (whisper tiny), `tts_d`
(OpenVoice) and `test_llm` (deepseek 7B) — and they share one Vector
Processor at `/dev/cavalry`. It does not run them in parallel. Every `Device
START` in `/tmp/log.txt` waits for the previous `END`.

Measured on 2026-09-14 with `tools/ambarella/vp_concurrency_probe.py`:

| | |
| --- | --- |
| ASR alone | 348 ms |
| ASR while the LLM is generating | 24 s |
| One LLM turn with ASR and TTS contending | 1455 s (24 min) |

All three on one VP is not a configuration, it is a queue.

Ambarella's recommendation is to move speech to the CPU, which is otherwise
idle — their document is `zipformer_piper_en_on_lychee.md`. This page is that
recommendation carried out for TTS.

## What was built

`ai_agents/agents/ten_packages/extension/sherpa_onnx_tts_python` — the 34th
TTS adapter in this repo, on the same `AsyncTTS2HttpExtension` base class as
the other 33. It runs a Piper VITS voice through
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) on the CPU and opens no
device node.

The graph `voice_assistant_sherpa_tts` is `voice_assistant_soniox_ambarella_llm`
with one field changed: the `tts` node's `addon`. Every connection addresses
that node by name, so the data flow is untouched. That is the point of the
node being an extension.

The resulting split:

| | Where |
| --- | --- |
| ASR | soniox, in the cloud |
| LLM | `test_llm` on the board, on the Vector Processor |
| TTS | sherpa-onnx on the board, on the CPU |

ASR stays in the cloud deliberately, so a failure in this graph isolates to
the one node that changed. Moving it to the board is separate work
(`sherpa_onnx_asr_python`), and sherpa-onnx's streaming Zipformer already
measured a real-time factor of 0.2 on one thread here.

## Measured on the board

Voice `vits-piper-zh_CN-huayan-medium`, one thread, 2026-09-15:

| | |
| --- | --- |
| Load | 2.0 s |
| Native rate | 22050 Hz (so rate conversion is always on the path) |
| Real-time factor | 0.34 – 0.38 |

| Characters | Synthesis | Audio | RTF |
| --- | --- | --- | --- |
| 3 | 0.29 s | 0.79 s | 0.37 |
| 7 | 0.52 s | 1.39 s | 0.38 |
| 28 | 1.80 s | 5.33 s | 0.34 |
| 57 | 3.87 s | 10.56 s | 0.37 |

Synthesis runs about three times faster than playback, and the factor is flat
across text length — cost is linear, so splitting long text buys no
throughput.

Thread count, measured separately: one thread 0.61 s, two 0.53 s, four 0.73 s
on four cores. Two are 13% faster than one and four are slower than one, and
those cores also carry the LLM server, the TEN runtime and the Go server. One
is the default.

### What a listener actually waits for

The real-time factor is the total. `OfflineTts.generate()` hands audio back
through a callback **once per sentence**, so a multi-sentence reply starts
playing long before synthesis finishes.

Measured with `tools/ambarella/probe_piper_tts.py` on a four-sentence reply:
first audio at 1.91 s against a 10.03 s total — roughly a fifth of the wait.
For comparison, ElevenLabs answered its first byte on this board in 361 ms and
454 ms.

Nothing is gained by splitting text before handing it over; the engine already
does it.

## The trap in sherpa-onnx's own documentation

`OfflineTts.generate()` documents its callback as:

> Return a non-zero value to stop generation early.

Measured against sherpa-onnx 1.13.8, **the opposite is true**: zero stops and
non-zero continues.

Implementing it as documented truncates every reply to its first sentence. No
exception, no log line, no error anywhere — just speech that ends early, which
is not a symptom anyone traces back to a callback's return value.

`const.py` names both values, and
`tests/test_engine_contract.py` pins them against the installed library so a
version that flips the polarity fails the suite rather than mangling speech on
the board. That test needs a voice, so it skips unless one is named:

```bash
SHERPA_ONNX_TTS_VOICE_DIR=~/piper_tts/vits/vits-piper-zh_CN-huayan-medium \
    ./tests/bin/start -k engine_contract
```

Run it after any sherpa-onnx upgrade.

## Two things the adapter has to get right

**`generate()` blocks its thread for the whole utterance.** It is a
synchronous C++ call, so it runs in a worker thread and each chunk is
converted and queued from there. Rate conversion happens on that thread too,
for the same reason.

**One yield becomes one `AudioFrame`.** The base class makes a frame per
yield, whatever its size, so a sentence handed over whole becomes a single
frame of a few hundred kilobytes where the rest of the chain is built around
20 ms. It also bounds barge-in at the wrong granularity: stopping the engine
leaves the sentence already synthesised in hand, and an interruption arriving
early in a long sentence would still talk over the user for the rest of it.
The frame loop reads the same cancellation flag between frames.

A sentence's trailing partial frame waits for the next sentence rather than
being padded or dropped, either of which is audible over a long reply.

## Language

A Piper VITS voice speaks one language — the models are trained per language,
which is the architecture rather than an oversight. Mandarin comes first
because that is what the board is being brought up in, and the graph's prompt
is Mandarin-only to match: an English reply read by a Mandarin phonemiser is
noise, and sounds like a broken model rather than a mismatched prompt.

| Option | Size | Rate | |
| --- | --- | --- | --- |
| `vits-piper-zh_CN-huayan-medium` | 67 MB | 22050 | Mandarin. In use. |
| `vits-melo-tts-zh_en` | 167 MB | 44100 | Mandarin and English in one VITS model. A `voice_dir` change, not a code change. Speed on this board unmeasured. |
| `matcha-icefall-zh-en` | 79 MB | — | Needs a separate vocoder file and a different config class. |
| `kokoro-int8-multi-lang-v1_1` | 147 MB | — | Needs per-language lexicons and a different config class. |

Only the first two load through `OfflineTtsVitsModelConfig`, which is why only
they are a path change.

Voices must come from sherpa-onnx's repackaged bundles rather than from
Hugging Face directly: the config wants `tokens.txt` and the espeak-ng data,
which the raw `.onnx` + `.onnx.json` pair does not carry.

## Installing it on the board

One script, from a fresh checkout to a verified install:

```bash
ai_agents/agents/scripts/install_board_arm64.sh --dry-run   # read the plan first
ai_agents/agents/scripts/install_board_arm64.sh             # install and verify
ai_agents/agents/scripts/install_board_arm64.sh --run       # ... and start it
```

It sequences the scripts that do the work rather than repeating them, so every
stage is somewhere you can go and read: the Zipformer model and the Piper voice,
then the tenapp, then the Go app and the Python packages, then the tests against
the real model. Each stage is idempotent, so a failed run can be restarted
without undoing anything.

It does not use `task install`. The registry has no arm64 build of `agora_rtc`
and the tenapp manifest pins it exactly, so tman -- which resolves the whole
dependency tree or none of it -- returns no model at all and reports only
`Dependency resolution failed without specific error details`. `--locked` fails
the same way, because the pinned version is the one with no arm64 build. The
dependency is dropped from the manifest for the resolve and restored on any
exit, and the prebuilt arm64 extension and SDK from
`ai_agents/agents/prebuilt/linux-arm64` are placed by hand afterwards, which is
what `install_agora_rtc_arm64.sh` has always done.

It resolves `TEN_PYTHON_LIB_PATH` itself, with the finder CI uses, because the
runtime dlopens its interpreter rather than linking it -- the library cannot be
read out of the binding's ELF, which is why that variable exists at all. On this
board `python3` is 3.13 and the runtime loads 3.12; pass `--python X.Y` to
choose another.

The LLM is not installed by it. That is the vendor's daemon on
`127.0.0.1:8080`; `tools/ambarella/check_llm_board.sh` compares what the board
has against what the kit's guide says it should.

It writes `ai_agents/.env` if there is none, moving `SERVER_PORT` to 8081.
`.env` is git-ignored, so a clone has none and every port falls back to its
default -- including the Go API server's 8080, which the LLM daemon already
holds. The playground then proxies `/api/agents/*` into the LLM and reports
`Parse Error: Invalid header token`, because what comes back is an LLM error
page whose second header line is the bare word `LLM`. An existing `.env` is
left alone and only warned about.

For the TTS extension alone, without the ASR side:

```bash
ai_agents/agents/scripts/install_sherpa_tts_board.sh --dry-run
ai_agents/agents/scripts/install_sherpa_tts_board.sh
```

**Every pip call is `--user`.** The board runs a vendor-patched Fedora with no
backup, and the example's own `install_python_deps.sh` defaults to
`uv pip install --system`, which writes into the same site-packages rpm owns.
The script sets `PIP_INSTALL_CMD` to redirect it. Nothing here calls `dnf`.

To see what is already installed without changing anything:

```bash
tools/ambarella/check_tts_env.sh
```

## Running it

```bash
cd ai_agents/agents/examples/voice-assistant
task run 2>&1 | tee /tmp/task_run.log
```

Pick a graph in the playground on port 3000:

| Graph | ASR | TTS |
| --- | --- | --- |
| `voice_assistant_sherpa_full` | sherpa-onnx, on the CPU | sherpa-onnx, on the CPU |
| `voice_assistant_sherpa_tts` | soniox, over the network | sherpa-onnx, on the CPU |

After a conversation, `tools/ambarella/check_asr_log.sh` reads the log and says
whether the ASR path behaved, so the checking is not a matter of remembering
which lines to grep for.

Adding a graph is not hot-reloadable: the frontend caches `/graphs`, so both
the server and the playground need a full restart after pulling this branch.

## Tools

| | |
| --- | --- |
| `tools/ambarella/check_tts_env.sh` | Report what is installed. Changes nothing. |
| `tools/ambarella/probe_piper_tts.py` | Measure a voice: load time, rate, RTF, time to first audio. Writes WAVs to listen to. |
| `tools/ambarella/vp_concurrency_probe.py` | Measure what the Vector Processor does with two and three models at once. |
| `tools/ambarella/verify_asr_board.sh` | Run the ASR tests on the board, under the interpreter the runtime loads, against the real model. Refuses a run with skips in it. |
| `tools/ambarella/check_asr_log.sh` | Read a `task_run.log` and say whether the ASR path behaved. |
| `tools/ambarella/check_llm_board.sh` | Compare the board's LLM against the vendor guide. Reads only. |
| `tools/ambarella/setup_tts_runtime_deps.sh` | Put the Python packages where the runtime will look, which is not where `pip install --user` puts them. |
| `ai_agents/agents/scripts/setup_cpu_asr_tts_arm64.sh` | Fetch sherpa-onnx, the Zipformer ASR model and the voices. |
| `ai_agents/agents/scripts/install_sherpa_tts_board.sh` | The TTS extension alone, in order. |
| `ai_agents/agents/scripts/install_board_arm64.sh` | **All of the above, in order, ending in a verified install.** |

## Status

Verified on the board on 2026-09-16.

`sherpa_onnx_asr_python`: 39 tests pass under `/usr/bin/python3.12`, the
interpreter the runtime loads, with no skips -- six of them put real audio
through the real model. pylint 10.00/10.

`sherpa_onnx_tts_python`: 24 unit tests and 3 engine-contract tests pass
against sherpa-onnx 1.13.8 with a real voice bundle.

A three-minute conversation through `voice_assistant_sherpa_full` transcribed
three utterances, each ended by the engine's own endpoint after 1280 ms of
trailing silence, each reaching the model once. Time from the last partial to
the final was 1.28 s, and from the final to the request 6-7 ms.

What has **not** yet been done on the board: the graph has not been brought up
end to end, and nobody has listened to the Mandarin voice through the RTC
chain. Until that happens, treat the numbers here as measurements of the
engine, not of the assistant.
