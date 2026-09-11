# ambarella_asr_python

ASR provider over `asr_d`, the resident Whisper-tiny daemon shipped in the
Ambarella AI Developer Kit. Runs natively on an N1-655 board.

## How it works

`asr_d` loads Whisper tiny into Vector Processor memory at startup, prints
`READY asr`, then answers `INFER <wav>` with `OK language=… text=…` in about
0.3 s. The extension spawns it as a child process, buffers incoming
`pcm_frame` audio, and issues one `INFER` per turn when the pipeline calls
`finalize()`.

Inference happens **only** on `finalize()`. `asr_d` emits no partial results,
so a rolling window would burn VP time and cut sentences with nothing to
reassemble them from. Put `ten_vad_python` in the graph so turns end.

## Prerequisites

On the board, before the agent starts:

```bash
cavalry_load -f /lib/firmware/cavalry.bin -r
chmod +x /home/lychee/asr_tts_demo/app_demo/asr_d
```

## Fixed properties of the daemon

| | Value | Configurable |
| --- | --- | --- |
| Input rate | 16000 Hz | No — compiled in |
| Format | PCM signed 16-bit, mono | No |
| Maximum audio per `INFER` | 30 s | No |
| Partial results | none | — |
| Language | fixed at startup | Per session only |

The 16 kHz input matches what RTC hands over after it decodes G.722, so
nothing is resampled on this path.

## Properties

| Property | Default | Notes |
| --- | --- | --- |
| `bin_path` | *required* | Absolute path to `asr_d` |
| `model_dir` | *required* | Becomes `--cavalry_dir` |
| `model_type` | `tiny` | Becomes `--type` |
| `tmp_dir` | `/tmp` | Holds the reused WAV |
| `min_audio_ms` | `200` | Shorter audio never reaches the VP |
| `load_timeout_s` | `180.0` | Model load allowance |
| `infer_timeout_s` | `30.0` | A breach means the daemon is wedged |
| `quit_timeout_s` | `5.0` | Then `kill()`, accepting a VP leak |
| `restart_max_attempts` | `3` | Each restart pays a full model load |
| `params` | see `property.json` | Expanded to `--key value` |

`params` is a pass-through onto `asr_d`'s startup flags: `language`,
`beam_size`, `no_speech_thres`, `log`. `cap_dev` is dropped deliberately —
it would enable `INFER_MIC`, which bypasses the TEN audio pipeline.

Language accepts TEN codes (`zh`, `zh-CN`, `en-US`) or the daemon's own words
(`chinese`, `english`, `auto`) and is reported back normalised.

## Tests

No hardware needed: `tests/stub_daemon.py` speaks the same line protocol, and
tests select its behaviour through `params.scenario`.

```bash
task test-extension EXTENSION=agents/ten_packages/extension/ambarella_asr_python
task test-extension-no-install EXTENSION=agents/ten_packages/extension/ambarella_asr_python -- -k test_daemon
```

## Design

`docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.md`
