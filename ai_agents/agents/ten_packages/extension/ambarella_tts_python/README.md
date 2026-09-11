# ambarella_tts_python

TTS provider over `tts_d`, the resident OpenVoice daemon shipped in the
Ambarella AI Developer Kit. Runs natively on an N1-655 board.

## How it works

`tts_d` loads OpenVoice into Vector Processor memory at startup, prints
`READY tts`, then answers `INFER <text> <out.wav>` with
`OK wav=… frames=…` in about 0.3 s. The extension spawns it as a child
process, reads the WAV back, resamples it and yields PCM in 20 ms chunks.

## Prerequisites

On the board, before the agent starts:

```bash
cavalry_load -f /lib/firmware/cavalry.bin -r
chmod +x /home/lychee/asr_tts_demo/app_demo/tts_d
```

`tts_d` also needs `libsndfile.so.1`, which `asr_d` does not — the vendor
README lists it as a shared prerequisite, which is misleading.

## Sample rate

`tts_d` writes a hardcoded 22050 Hz PCM16 mono WAV: the constant and the
`SF_FORMAT_WAV|SF_FORMAT_PCM_16` format flag each appear once in its `.text`
with no flag reaching them.

The transport is 16 kHz G.722, and the RTSA SDK is told its PCM input rate
**once** at service initialisation rather than per frame. A session declared at
16000 would read 22050-sampled data as 16000 and play it slow and low-pitched
with no error raised, so this extension converts to 16000 itself using
`scipy.signal.resample_poly(x, 320, 441)` — an exact ratio. G.722's passband
tops out near 7 kHz, so the downsample discards nothing the codec would have
kept.

The WAV header is checked and the ratio recomputed from it on every response,
but a rate other than 22050 is only ever logged once per client lifetime —
`_warned_rate` latches after the first warning, so a persistently wrong rate
does not spam the log on every subsequent response.

## Properties

| Property | Default | Notes |
| --- | --- | --- |
| `bin_path` | *required* | Absolute path to `tts_d` |
| `model_dir` | *required* | Becomes `--model_dir` |
| `tmp_dir` | `/tmp` | Holds the reused WAV |
| `max_chars` | `200` | Punctuation split for one long sentence |
| `output_sample_rate` | `16000` | Declared to the pipeline |
| `load_timeout_s` | `180.0` | Model load allowance |
| `infer_timeout_s` | `30.0` | A breach means the daemon is wedged |
| `quit_timeout_s` | `5.0` | Then `kill()`, accepting a VP leak |
| `restart_max_attempts` | `3` | Each restart pays a full model load |
| `dump` | `false` | Dump synthesised PCM for debugging |
| `dump_path` | `/tmp` | Directory the dump file is written into |
| `params` | see `property.json` | Expanded to `--key value` |

`params` carries `speaker_id` (0-9, the voice), `rand_seed` and `log`. All
three are startup flags, so **the voice is fixed for a session** and cannot be
changed per request.

## Barge-in

`cancel()` sets a flag and stops yielding; it never kills the daemon. `tts_d`'s
`INFER` is not interruptible, but it only runs ~0.3 s — whereas killing the
process would cost a full model reload on every barge-in.

## Tests

```bash
task test-extension EXTENSION=agents/ten_packages/extension/ambarella_tts_python
```

## Design

`docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.md`
