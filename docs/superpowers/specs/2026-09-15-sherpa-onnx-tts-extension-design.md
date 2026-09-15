# sherpa_onnx_tts_python: speech synthesis on the board's CPU

Replaces a cloud TTS provider with one that runs on the Ambarella N1-655
itself, without touching the Vector Processor that the on-board LLM needs.

## Why

Measured on an N1-655 on 2026-09-14, the board's three on-device models share
one Vector Processor and it does not run them in parallel: every `Device
START` in `/tmp/log.txt` waits for the previous `END`. ASR under a generating
LLM went from 348 ms to 24 s, and one LLM turn reached 24 minutes. Ambarella's
recommendation is to move speech to the CPU, which is otherwise idle.

Verified on the board on 2026-09-15: sherpa-onnx transcribes at a real-time
factor of 0.2 on one thread, and Piper synthesises both languages. Neither
opens `/dev/cavalry`.

The Mandarin voice, measured on the board the same day at one thread: it loads
in 2.0 s, emits 22050 Hz, and synthesises at a real-time factor of 0.34 to
0.38 -- about three times faster than playback. The factor is flat from 3
characters to 57, so cost is linear in text length and splitting long text
buys no throughput.

This spec covers TTS only. ASR stays on soniox for now, so a failure in the
resulting graph isolates to the one node that changed.

## What it is

The 34th TTS vendor adapter in this repo. `AsyncTTS2HttpExtension` already
carries the protocol -- `tts_text_input` in, `pcm_frame` out, the
`tts_audio_start` / `tts_audio_end` pair, flush on barge-in, the metrics. The
adapter supplies only what is specific to this engine.

The graph's data flow does not change. One `addon` field does.

## Engine

`pip install sherpa-onnx` — PyPI publishes `manylinux2014_aarch64` wheels, so
the board needs no toolchain. The package carries both ASR and TTS; the
board's separately built CLI was configured with `SHERPA_ONNX_ENABLE_TTS=OFF`
and is not used here.

Voices are Piper VITS models, taken from sherpa-onnx's repackaged bundles
rather than from Hugging Face directly: `OfflineTtsVitsModelConfig` wants
`tokens` and `data_dir` (espeak-ng data), which the raw `.onnx` +
`.onnx.json` pair on Hugging Face does not carry.

| Voice | Bundle | Size | Rate |
| --- | --- | --- | --- |
| Mandarin | `vits-piper-zh_CN-huayan-medium` | 67 MB | 22050 Hz |
| Mandarin + English | `vits-melo-tts-zh_en` | 167 MB | 44100 Hz |

A Piper voice speaks one language: the VITS models are trained per language,
which is the architecture rather than an oversight. One voice carrying both
is available -- `vits-melo-tts-zh_en` -- and because it is also a VITS model
it loads through the same `OfflineTtsVitsModelConfig`. Moving to it is a path
change, not a code change. Its speed on this board is unmeasured, and it is
two and a half times the size, so Mandarin alone comes first.

The multilingual alternatives that are *not* VITS are out of scope: Matcha
(`matcha-icefall-zh-en`, 79 MB) needs a separate vocoder file, and Kokoro
(`kokoro-int8-multi-lang-v1_1`, 147 MB) needs per-language lexicons. Both use
a different config class.

English also has 16000 Hz voices (`-low`, `-x_low`); Mandarin does not, so
rate conversion is on the path from the start rather than deferred.

## Behaviour

### Configuration

1. The voice directory must exist and hold its model, tokens and espeak-ng
   data. Missing, the extension fails at start rather than at the first
   spoken sentence.
2. Output sample rate is configurable, defaulting to 16000 — what the RTC
   chain is declared with.
3. Speed and thread count are configurable. One thread is the default:
   measured on the board, two are 13% faster and four are slower than one,
   and those cores also carry the LLM server, the TEN runtime and the Go
   server.

### Synthesis

4. Text becomes PCM.
5. **The rate comes from the model, not from configuration.**
   `OfflineTts.sample_rate` reports it, and conversion runs only when it
   differs from the target — so a 16000 Hz voice costs nothing.
6. **`generate()` must not run on the event loop.** It is a synchronous C++
   call; it runs in a worker thread.
7. Audio is emitted in chunks as it is produced, through `generate()`'s
   callback, rather than accumulated and cut up afterwards. The engine calls
   back once per sentence, so a multi-sentence reply starts playing after its
   first sentence: measured at 1.91 s against a 10.03 s total, roughly a fifth
   of the wait. Splitting text ourselves would not improve on this, which is
   the other half of why long-text splitting is out of scope.
8. Empty or whitespace-only text yields no audio and does not reach the
   engine. `main_control` sends an empty string to close a turn
   (`main_python/extension.py:185`), so this is a normal event, not an error.

### Interruption

9. Barge-in stops generation rather than discarding its result: the callback
   returns **zero** to stop and non-zero to continue, which ends `generate()`
   early. The model stays loaded -- reloading costs 2.0 s.

   This is the reverse of what `generate()`'s own docstring states ("Return a
   non-zero value to stop generation early"), measured against sherpa-onnx
   1.13.8. Believing the docstring truncates every reply to its first
   sentence, silently and without error. A test pins the polarity against the
   installed library so a version that flips it fails the suite instead of
   mangling speech on the board.

### Lifecycle

10. The model loads once and is released on stop.
11. Loading is warmed in the background so the first sentence does not pay
    for it, following `ambarella_tts_python`, whose warm-up races its own
    teardown if the two are not drained in order.

## What the engine provides that the precedent had to build

`ambarella_tts_python` drives a daemon over a line protocol: it writes a WAV
file, reads it back, and cuts it into chunks. That brings process lifetime,
timeouts, crash restarts, and a VP that is only released on `QUIT` — the
source of most of its defects.

None of that applies here. `generate()`'s callback delivers audio
incrementally and accepts a stop signal, so chunking and cancellation are the
engine's, not ours. There is no subprocess.

## Not in scope

- ASR. `sherpa_onnx_asr_python` is a separate piece of work.
- Anything but VITS. Supporting `OfflineTtsVitsModelConfig` alone covers both
  the Mandarin voice this starts with and the bilingual one it can move to.
- English. Mandarin first; a second language is either a second node or the
  bilingual model, and that choice is better made after the first one is
  heard on the board.
- Long-text splitting. The engine has no length limit that requires it,
  synthesis cost is linear in length, and the per-sentence callback already
  delivers the first audio early.

## Testing

Unit tests cover what does not need a model: rate conversion, text
sanitising, empty input, and that cancellation stops generation rather than
discarding output. Synthesis itself is exercised on the board, where the
voices are.

One test does need the library but not a voice: the callback polarity in
behaviour 9. It is the one contract this adapter takes from sherpa-onnx that
the documentation gets wrong, and its failure mode -- speech cut off after one
sentence -- is not one a reader of the code would suspect.

The rate conversion uses `resample_poly`, whose FIR anti-aliasing filter
matters here: this is a downsample, and without it the 8-11 kHz content folds
back audibly.
