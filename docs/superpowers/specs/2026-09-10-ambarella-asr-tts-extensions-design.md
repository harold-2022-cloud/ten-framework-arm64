# Ambarella ASR/TTS extensions — design

Two TEN extensions wrapping the resident speech daemons shipped in the Ambarella
AI Developer Kit, plus the example graph that puts them on a board with the
existing LLM provider.

Written against `feat/arm64-native-build`, 2026-09-10. Every claim about the
daemons was checked against the binaries themselves — decoded at instruction
level where the vendor documentation is silent. Claims that could not be
verified say so rather than guessing; §9 lists them.

**Contents**

| § | Covers |
| --- | --- |
| 1 | Purpose and scope |
| 2 | What the daemons actually are (verified) |
| 3 | Decision: one daemon per extension |
| 4 | Package layout |
| 5 | `daemon.py` — the protocol client |
| 6 | `ambarella_asr_python` |
| 7 | `ambarella_tts_python` |
| 8 | Error handling and recovery |
| 9 | Risks and unverified claims |
| 10 | Testing strategy |
| 11 | Example graph |
| 12 | Verification log |

---

## 1. Purpose and scope

`ambarella_ai_developer_kit_doc/asr_tts_20260907/app_demo/` ships two aarch64
binaries — `asr_d` (Whisper tiny) and `tts_d` (OpenVoice) — that hold a model
resident in Vector Processor memory and answer single-shot inference commands
over a line protocol on stdin/stdout. This design wraps each as a TEN extension
so a voice agent can run entirely on an N1-655 board.

**In scope**

- `ambarella_asr_python` — an `AsyncASRBaseExtension` provider over `asr_d`.
- `ambarella_tts_python` — an `AsyncTTS2HttpExtension` provider over `tts_d`.
- One example graph wiring both to the existing `ambarella_llm2_python`.

**Out of scope**

- `ambarella_llm2_python` already exists and talks to a different board
  subsystem (the `run_llm_demo.sh` HTTP server). It is untouched here.
- Microphone and speaker extensions, board provisioning, model installation.
- The `asr_chinese_20260909/openvoice_n1-655.zip` package — a separate,
  differently-shaped release (`bin/openvoice` + prepared input tensors) that
  does not speak this protocol.

**Settled by prior decision**

| Question | Decision |
| --- | --- |
| Where the agent runs | Natively on the board (Lychee OS, aarch64) |
| ASR audio source | The TEN pipeline, via `INFER <wav>` — not the daemon's own ALSA capture |
| Utterance boundary | `ten_vad` in the graph drives `finalize()` |
| Deliverable | Both extensions plus one example graph |

---

## 2. What the daemons actually are (verified)

### 2.1 Line protocol

One command per line on stdin; one terminal line per command on stdout. Both
daemons print a readiness token once the model is resident, and release VP
memory only on `QUIT`.

**`asr_d`**

| stdin | stdout |
| --- | --- |
| *(startup)* | `READY asr` |
| `INFER <wav_path>` | `OK language=<lang> text=<text>` |
| `INFER_MIC <duration_ms>` | `OK recording <ms> ms from <dev>`, then `OK language=… text=…` |
| `QUIT` | `OK bye` |

Error lines, verbatim from the binary: `ERR no_mic pass --cap_dev <alsa_device>`,
`ERR duration_ms range 100-30000`, `ERR wav_format`, `ERR audio_too_short`,
`ERR no_audio`, `ERR capture`, `ERR no speech.`, `ERR oom`, `ERR infer`,
`ERR init`, `ERR cannot_read %s`, `ERR unknown_cmd`.

**`tts_d`**

| stdin | stdout |
| --- | --- |
| *(startup)* | `READY tts` |
| `INFER <text> <out.wav>` | `OK wav=<path> frames=<n>` |
| `QUIT` | `OK bye` |

Errors: `ERR usage INFER <text> <out.wav>`, `ERR sndfile %s`, `ERR init`,
`ERR unknown_cmd`.

`INFER` for TTS splits on whitespace and takes the **last token** as the output
path. The text therefore may contain spaces but must not contain a newline —
see §7.

### 2.2 Startup flags

Flags take effect for the life of the process. There is no per-request
parameter on either daemon.

| `asr_d` | Default | Notes |
| --- | --- | --- |
| `--cavalry_dir <dir>` | *(required)* | Whisper model directory |
| `--type <size>` | — | `tiny` for the shipped model set |
| `--language <lang>` | `chinese` | Full words: `chinese`, `english`, `auto` |
| `--beam_size <n>` | `5` | 1–20 |
| `--no_speech_thres <f>` | `0.6` | 0.0–1.0; lower is more sensitive |
| `--cap_dev <dev>` | *(unset)* | Enables `INFER_MIC`; unused by this design |
| `--log <0-4>` | `2` | 0 None, 1 Error, 2 Notice, 3 Debug, 4 Verbose |

| `tts_d` | Default | Notes |
| --- | --- | --- |
| `--model_dir <dir>` | *(required)* | OpenVoice model directory |
| `--speaker_id <0-9>` | `0` | Voice selected from `embed.bin` |
| `--rand_seed <n>` | `-1` | `-1` randomises per synthesis |
| `--log <0-4>` | `2` | As above |

### 2.3 Audio formats — decoded, not documented

The vendor README states the rates in prose. Because the extension must
declare a rate to the pipeline and a wrong value fails silently (audio plays at
the wrong pitch with no error), both were confirmed at instruction level.
`objdump` in the dev environment has no aarch64 backend, so `.text` was scanned
for `MOVZ`/`MOVK` immediates directly.

`tts_d`, at `.text` offset `0x21c0`, builds `SF_INFO` for `sf_open`:

```
movz x6, #22050          samplerate = 22050
movk x6, #1, lsl #32     channels   = 1
str  x6, [sp, #56]       → SF_INFO.samplerate, SF_INFO.channels
movz w4, #2              SF_FORMAT_PCM_16
movk w4, #1, lsl #16     | SF_FORMAT_WAV   → 0x010002
str  w4, [sp, #64]       → SF_INFO.format
```

`asr_d` carries the symmetric constant at `0x2984`: `movz w3, #16000`.

| | `asr_d` | `tts_d` |
| --- | --- | --- |
| Sample rate | 16000 | 22050 |
| Channels | 1 | 1 |
| Sample format | PCM S16 | PCM S16 (`SF_FORMAT_WAV\|SF_FORMAT_PCM_16`) |
| Container | bare WAV in | WAV out |
| Configurable | **No** — one occurrence in `.text` | **No** — one occurrence in `.text` |

Each constant appears exactly once in `.text`, and no flag reaches it. The rates
are fixed properties of the binaries. `tts_d` calls `sf_write_float`, so the
model emits float and libsndfile narrows to PCM16 on write.

### 2.4 Runtime dependencies

Both link the board's EazyAI runtime: `libamba-eazyai-n1_655.so.0`,
`-inf-`, `-io-`, `-utils-`, `-postprocess-`. **`tts_d` additionally needs
`libsndfile.so.1`; `asr_d` does not** — the vendor README lists it under shared
prerequisites, which is misleading.

Cavalry firmware must be loaded before either starts:

```bash
cavalry_load -f /lib/firmware/cavalry.bin -r
```

---

## 3. Decision: one daemon per extension

**Chosen.** `ambarella_asr_python` owns an `asr_d` child process;
`ambarella_tts_python` owns a `tts_d` child process. Each spawns via
`asyncio.create_subprocess_exec` and binds the daemon's lifetime to its own.
The protocol client lives in a per-package `daemon.py` of roughly 120 lines.

This matches TEN's one-extension-one-provider model: each package imports its
own interface JSON, installs standalone, and is testable on its own with
`task test-extension`. `app_demo/test_alternate.py` already demonstrates both
daemons resident simultaneously, so no coordination between them is required at
startup.

The cost is that `daemon.py` is duplicated across two packages. That is the
house pattern — `deepgram_asr_python` and `deepgram_tts` each carry their own
client for the same vendor — and TEN extensions are deliberately
self-contained units with their own `pyproject.toml` and `requirements.txt`.

**Rejected: a shared system package.** Putting the protocol client in
`ten_packages/system/ambarella_ea/` removes the duplication, but
`ten_packages/system/` in this repo holds no local source at all: it is
entirely registry dependencies materialised by `tman install` (`ten_ai_base`
arrives that way). A local system package would need publishing and version
management to save 120 lines.

**Rejected: one extension owning both daemons.** A single extension can import
only one interface. `asr_result` leaving and `tts_text_input` arriving belong to
different graph nodes, and the flush/barge-in semantics of the two differ. This
fights the runtime's message routing rather than using it.

---

## 4. Package layout

```
ai_agents/agents/ten_packages/extension/
├── ambarella_asr_python/
│   ├── addon.py            @register_addon_as_extension("ambarella_asr_python")
│   ├── extension.py        AsyncASRBaseExtension
│   ├── daemon.py           line-protocol client
│   ├── config.py           pydantic config, params pass-through
│   ├── const.py
│   ├── manifest.json       imports asr-interface.json
│   ├── property.json
│   ├── pyproject.toml  requirements.txt  README.md
│   └── tests/             stub daemon + state-machine tests
└── ambarella_tts_python/
    ├── addon.py            @register_addon_as_extension("ambarella_tts_python")
    ├── extension.py        AsyncTTS2HttpExtension
    ├── ambarella_tts.py    AsyncTTS2HttpClient subclass
    ├── daemon.py           same shape as above
    └── … (as above, imports tts-interface.json)
```

Startup flags follow the repo's `params` pass-through convention, as
`whisper_stt_python` does: a `params` dict is expanded to `--key value` pairs,
so `{"language": "chinese", "beam_size": 5, "log": 1}` becomes
`--language chinese --beam_size 5 --log 1`. `bin_path` and `model_dir` are
separate top-level properties because they point outside the tenapp, at
`/home/lychee/asr_tts_demo/…`.

### Property defaults

Stated explicitly so the implementer does not have to choose them. Every timeout
and cap below is a property, not a constant.

| Property | ASR | TTS | Notes |
| --- | --- | --- | --- |
| `bin_path` | *required* | *required* | Absolute path to `asr_d` / `tts_d` |
| `model_dir` | *required* | *required* | Becomes `--cavalry_dir` / `--model_dir` |
| `load_timeout_s` | `180.0` | `180.0` | Matches the vendor demo scripts |
| `infer_timeout_s` | `30.0` | `30.0` | ~100x the observed 0.3 s; a breach means the daemon is wedged |
| `quit_timeout_s` | `5.0` | `5.0` | Then `kill()`, accepting the VP leak |
| `restart_max_attempts` | `3` | `3` | Each attempt pays a full model load |
| `min_audio_ms` | `200` | — | The real floor is unverified — §9 |
| `max_chars` | — | `200` | Punctuation-split guard for one long sentence |
| `output_sample_rate` | — | `16000` | Declared to the pipeline; ratio derived from the WAV header |
| `tmp_dir` | `/tmp` | `/tmp` | Holds the reused WAV |
| `params` | `{language: chinese, beam_size: 5, no_speech_thres: 0.6, log: 1}` | `{speaker_id: 0, rand_seed: -1, log: 1}` | Expanded to `--key value` |

Three names must match exactly or the graph fails to load silently: the
`@register_addon_as_extension` argument, the `name` field in `manifest.json`,
and the `addon` field of the graph node.

---

## 5. `daemon.py` — the protocol client

| Method | Behaviour |
| --- | --- |
| `start()` | `create_subprocess_exec(bin_path, *flags, stdin=PIPE, stdout=PIPE, stderr=STDOUT)`; read lines until the readiness token; raise on `ERR` or timeout |
| `request(line, timeout)` | Under an `asyncio.Lock`: write the line, `drain()`, then read until the first line starting `OK ` or `ERR` |
| `stop()` | Send `QUIT`, await `OK bye`, `await proc.wait()`; `kill()` only on timeout |
| `alive` | `proc is not None and proc.returncode is None` |

Three rules the implementation must honour.

**stdout carries vendor noise, so the reader must tolerate it.** EazyAI logs to
the same stdout at the default `--log 2` (Notice). All four vendor demo scripts
run with `stderr=STDOUT` and skip every line that is not `READY`/`OK`/`ERR`.
`request()` therefore treats any line not beginning `OK ` or `ERR` as a vendor
log, recorded at debug level under `LOG_CATEGORY_VENDOR`. `property.json`
defaults to `log: 1` (Error) to keep the channel quiet.

**Model loading must not block `on_start`.** The model sets are 150 MB (Whisper)
and 84 MB (OpenVoice); the vendor scripts allow a 180-second load timeout. TEN
spawns one worker process per session from `POST /start`, so a synchronous load
would stall session setup for tens of seconds. `start()` is launched as a
background task and returns immediately; `request()` awaits readiness. The load
then overlaps RTC join and the greeting, and the first turn pays only the
remainder.

**`on_stop()` must send `QUIT` and await exit.** VP memory is released only on
the clean path (vendor README §7). This also satisfies the framework rule that
extensions run off the main thread and must not install signal handlers or
`atexit` hooks — cleanup belongs in `on_stop()`.

---

## 6. `ambarella_asr_python`

Overrides `vendor()`, `start_connection()`, `stop_connection()`,
`is_connected()`, `input_audio_sample_rate()`, `buffer_strategy()`,
`send_audio()` and `finalize()`, following `whisper_stt_python/extension.py`.

```
audio_frame ──► send_audio()      accumulate a bytearray only; no inference
                     │
ten_vad silence ──► finalize()    the single inference trigger
                     │
                     ├─ snapshot and clear the buffer
                     ├─ shorter than min_audio_ms → finalize_end, skip the VP
                     ├─ write WAV to the reusable temp path
                     ├─ request("INFER <path>")   ≈ 0.3 s
                     ├─ parse OK language=… text=…
                     ├─ send_asr_result(final=True)
                     └─ send_asr_finalize_end()
```

**Deliberately unlike `whisper_stt_python`.** That extension transcribes every
accumulated second inside `send_audio()` and clears its buffer
(`whisper_client.py:102`). Against `asr_d` that would burn 0.3 s of VP per
second of speech, chop sentences at window edges, and — because this daemon
emits no partial results — offer nothing to accumulate them back from.
Inference happens only in `finalize()`.

**The 30-second ceiling.** 16 kHz mono S16 is 32,000 B/s, so the daemon's
window is 960,000 bytes. On reaching it the extension **forces an early
inference and keeps listening**, rather than dropping the oldest audio:
discarding loses speech the user produced, while an early inference merely
splits a long monologue into two results, both final and both correct.
`buffer_strategy()` returns `ASRBufferConfigModeKeep(byte_limit=960_000)`, so
the base class's own buffer agrees with the daemon's limit. It does *not*,
however, catch the frames that arrive while the model is still loading:
`ten_ai_base` only consults `buffer_strategy()` while `is_connected()` is
`False`, and `is_connected()` returns `True` the instant the process spawns —
tens of seconds before the model is resident. The extension's own `_buffer`
is what catches those early frames instead.

**`ERR no speech.` is not an error.** It is the daemon's normal answer to
silence: emit no result and no `ModuleError`, only `finalize_end`. Genuine
failures (`ERR infer`, `ERR wav_format`, `ERR oom`, timeout, dead process) go to
`send_asr_error` with `NON_FATAL_ERROR` and vendor info.

**`send_asr_finalize_end()` fires on every path** — success, empty result,
error, timeout, crash. Any path that misses it leaves `main_control` waiting on
that turn forever. This is the invariant most worth pinning with tests.

**Parsing and temp files.** The text in `OK language=%s text=%s` may contain
spaces and `=`, so it is taken as `split("text=", 1)[1]` (as the vendor scripts
do), with the language read between `language=` and ` text=`. The temp WAV is
one fixed path per instance (`<tmp_dir>/ambarella_asr_<uuid8>.wav`), overwritten
each turn and unlinked in `on_stop()`; `request()` already serialises access
under its lock. Writing uses the stdlib `wave` module at 1 channel, 2 bytes,
16000 Hz.

**Language is fixed for the session.** `--language` is a startup flag, so one
process serves one language. The property accepts TEN codes and maps them
(`zh-CN`/`zh` → `chinese`, `en-US`/`en` → `english`, `auto` passes through),
reporting the normalised TEN code back on `ASRResult` as
`whisper_stt_python`'s `normalized_language` does.

---

## 7. `ambarella_tts_python`

Extends `AsyncTTS2HttpExtension` — the base `polly_tts` uses, whose contract is
transport-agnostic despite the name. Four methods: `create_config()`,
`create_client()`, `vendor()`, `synthesize_audio_sample_rate()`. The work is in
the client's `get(text, request_id)`, an async generator of
`(bytes | None, TTS2HttpResponseEventType)`.

```
get(text, request_id)
  ├─ sanitise text (mandatory, below)
  ├─ empty → yield None, END
  ├─ request("INFER <text> <out_path>")   ≈ 0.3 s
  ├─ parse OK wav=… frames=…
  ├─ read PCM back with the wave module (no 44-byte assumption)
  ├─ yield (pcm_chunk, RESPONSE) in ~20 ms units
  │     check _is_cancelled before each chunk → yield None, FLUSH; return
  └─ yield None, END
```

**Sanitising the text is mandatory, not defensive.** The protocol is one command
per line with the last whitespace token as the output path. A single `\n` in
LLM output would end the command early and leave the remainder to be read as a
second, unparseable command — permanently desynchronising every subsequent
request and response on that pipe. The first action in `get()` is to fold all
`\r`, `\n`, `\t` and runs of whitespace into single spaces; if nothing remains,
return `END`.

**`cancel()` does not kill the process.** `tts_d`'s `INFER` cannot be
interrupted, but it only runs ~0.3 s. Barge-in therefore sets `_is_cancelled`,
lets synthesis finish, discards the audio and closes the turn with `FLUSH`.
Killing the daemon on barge-in would cost a full model reload, and barge-in is
routine in a voice pipeline.

### Sample rate: one conversion, in the extension

The transport is 16 kHz G.722 end to end, which settles both directions.

**Inbound needs no conversion at all.** RTC receives G.722, decodes it to 16 kHz
PCM and hands that to the pipeline; `asr_d` has 16000 compiled in (§2.3). The
two ends already agree, so `input_audio_sample_rate()` returns 16000 and nothing
is resampled on the way to ASR.

**Outbound needs exactly one conversion, and only the extension can do it.**
`tts_d` emits 22050 and cannot be told otherwise. The RTSA SDK is told its PCM
input rate **once**, at service initialisation: `audio_codec_option_t` carries
`audio_codec_type`, `pcm_sample_rate`, `pcm_channel_num` and `pcm_duration`
together (`agora_rtc_api.h:634-654`), while the per-frame struct handed to
`agora_rtc_send_audio_data` carries only a `data_type`
(`audio_frame_info_t`, `agora_rtc_api.h:517-522`). A session declared at 16000
therefore reads 22050-sampled data as 16000: 22050 samples play as 1.378
seconds — slow and low-pitched, with no error raised anywhere.

The extension therefore resamples 22050 to 16000 with
`scipy.signal.resample_poly(x, 320, 441)` — the ratio is exact, 16000/22050
reducing to 320/441 — and `synthesize_audio_sample_rate()` returns 16000.
`numpy` and `scipy` become dependencies; four extensions in this repo already
carry scipy.

**The conversion costs nothing the codec would have kept.** G.722 is a 16 kHz
wideband codec (`AUDIO_DATA_TYPE_G722 = 5`, documented as "G722, sample=16k",
`agora_rtc_api.h:439`) whose passband tops out near 7 kHz. Everything the
downsample removes, G.722 would have discarded regardless.

`resample_poly` is specified rather than the `np.interp` used elsewhere in this
repo (`conversation_recorder/audio_mixer.py:38`,
`qwen3_tts_python/qwen3_tts.py:204`) because linear interpolation carries no
anti-aliasing filter, and this is a downsample: without the FIR stage the
8-11 kHz content folds back into the audible band as a persistent hiss.

The WAV header is still read and checked on every response. A header that is not
22050 means the binary or the model was swapped, which would make a fixed
320/441 ratio wrong — so the ratio is recomputed from the header rate, and the
mismatch is logged loudly once.

**Long text relies on upstream sentence splitting, with an internal guard.**
`tts_text_input` already arrives per sentence, so a `get()` call is normally one
sentence. For a single very long sentence the whole synthesis latency would
land before the first frame, so `get()` splits on punctuation at `max_chars`,
inferring and yielding per piece — consecutive single-shot syntheses producing
a streaming impression.

**Voice is fixed for the session,** as language is on the ASR side:
`--speaker_id` and `--rand_seed` are startup flags, not per-request parameters.

---

## 8. Error handling and recovery

| Class | Detection | Response |
| --- | --- | --- |
| Load failure | `ERR init`, bad model path, Cavalry not loaded, VP OOM | `FATAL_ERROR`; do not retry — retrying cannot succeed |
| Inference failure | `ERR infer`, `ERR wav_format`, `ERR oom`, `ERR sndfile` | `NON_FATAL_ERROR`; process is alive, next turn proceeds |
| Process death | `returncode is not None` | Restart with exponential backoff, **capped** |

The restart cap matters: every restart pays a full model load, so an uncapped
retry loop would pin the board in a reload cycle instead of failing visibly.
The backoff follows `whisper_stt_python/reconnect_manager.py`.

---

## 9. Risks and unverified claims

**VP concurrency is unverified — the one real unknown.**
`app_demo/test_alternate.py` proves the two daemons can be **resident**
simultaneously and infer **alternately**. It never exercises **simultaneous**
inference. A voice pipeline does: a user barging in while TTS is synthesising
puts an ASR `INFER` on top of a TTS `INFER`. Outcomes range from both slowing
down to VP contention failing outright.

No cross-extension lock is specified. Both extensions live in the same worker
process, so a module-level `asyncio.Lock` would be technically possible, but it
would make barge-in ASR wait for TTS synthesis — paying latency for a problem
not yet observed. **Instead this design carries a required measurement:** after
implementation, run a concurrency test on the board (trigger ASR mid-synthesis)
and decide on a lock from the result. This is a task, not a judgement call left
to the implementer.

**Port 8080 collides on the board.** The board's LLM demo HTTP server binds
8080, and TEN's Go API server defaults to the same port
(`ai_agents/server/main.go:92`, from `SERVER_PORT`). Running the agent natively
on the board hits this directly, and the vendor's documented
`run_llm_demo.sh` options (`ip`, `run_mode`, `model_type`, `bsize`,
`model_path`, `max_user`, `help`) contain no port flag. The resolution is
therefore on TEN's side: set `SERVER_PORT` to something else for on-board runs.

**No `conversation_recorder` concern remains.** An earlier revision of this
design passed 22050 through, which would have left `audio_mixer` to downsample
it with `np.interp` and no anti-aliasing filter
(`conversation_recorder/audio_mixer.py:38`). Emitting 16000 removes that path
entirely — the mixer and the transport now agree with the extension.

**`asr_d`'s minimum audio length is not documented.** `ERR audio_too_short`
exists, and mic mode's floor is 100 ms (`ERR duration_ms range 100-30000`), but
the file-mode floor is unstated. `min_audio_ms` is therefore a property,
defaulting to 200 ms, to be tuned once the real floor is observed.

**The vendor README's dependency list is wrong** about `libsndfile.so.1` being
needed by both daemons (§2.4). Harmless, but do not treat that README as
authoritative for `asr_d`'s runtime requirements.

---

## 10. Testing strategy

The protocol boundary is a child process, so pointing `bin_path` at a Python
stub that speaks the same line protocol exercises the whole state machine on
x86 CI, with no aarch64 hardware. This is the concrete payoff of §3's choice.

| Scenario the stub plays | Asserts |
| --- | --- |
| `READY` then `OK language=… text=…` | Happy path; result reaches `send_asr_result` |
| No `READY` within the timeout | Load failure is `FATAL_ERROR`, not a hang |
| `ERR no speech.` | No `ModuleError`; `finalize_end` still fires |
| EazyAI noise interleaved on stdout | Noise is skipped, not parsed as a result |
| Process exits mid-inference | Restart with backoff; the cap is honoured |
| Audio shorter than `min_audio_ms` | No `INFER` is issued at all |
| Buffer reaches 960,000 bytes | Early inference fires; capture continues |
| TTS: text containing `\n` | Sanitised; the pipe stays synchronised |
| TTS: WAV header at a rate other than 22050 | Mismatch logged; rate reported from the header |
| TTS: `cancel()` mid-stream | `FLUSH`; the daemon is not killed |

**Five separate tests pin `send_asr_finalize_end()`** — one per path: success,
empty result, error, timeout, crash. This is the invariant whose failure mode
(a permanently stalled turn) is worst and least visible.

---

## 11. Example graph

`ai_agents/agents/examples/voice-assistant-ambarella/`, with the node chain:

```
mic → ten_vad → ambarella_asr_python → main_control
                                          ↓
speaker ← ambarella_tts_python ← ambarella_llm2_python
```

Two files change: `tenapp/property.json` for `predefined_graphs[]` (`nodes` plus
typed `connections`), and `tenapp/manifest.json` for three path dependencies.
No glue code — wiring an extension into a pipeline is JSON.

The LLM node must be named `llm`: `main_control` addresses `chat_completion` and
`abort` to that name. Adding a graph is not hot-reloadable — both the server and
the playground need a restart, because the frontend caches `/graphs`.

---

## 12. Verification log

| Claim | How it was checked |
| --- | --- |
| Protocol commands, responses, error strings | `strings` on both binaries |
| Startup flag names and validation bounds | `strings`, exact-match on option tokens |
| `tts_d` output 22050 / mono / `WAV\|PCM_16` | `MOVZ`/`MOVK` decode of `SF_INFO` setup at `.text` `0x21c0` |
| `asr_d` input 16000 | `MOVZ w3, #16000` at `.text` `0x2984` |
| Rates are not configurable | Each constant occurs once in `.text`; no flag reaches it |
| `tts_d` needs `libsndfile`, `asr_d` does not | `readelf -d` `NEEDED` entries on both |
| `tts_d` writes float through libsndfile | `sf_write_float` in the dynamic symbol table |
| `AudioFrame` carries its own sample rate | `core/include/ten_runtime/msg/audio_frame/audio_frame.h:39-41` |
| `AsyncTTS2HttpClient.get()` signature | `sarvam_http_tts/sarvam_tts.py:70` |
| ASR base class contract | `whisper_stt_python/extension.py` |
| `AUDIO_CODEC_TYPE_G722 = 2` is selectable | `agora_rtc_api.h:397-414` |
| G.722 is a 16 kHz codec | `agora_rtc_api.h:439` — "G722, sample=16k" |
| The SDK encodes pushed PCM to the chosen codec | `agora_rtc_api.h:638` |
| PCM rate and channels are declared once at init | `audio_codec_option_t`, `agora_rtc_api.h:634-654`; per-frame struct is `data_type` only, `:517-522` |
| `pcm_frame` is 16-bit mono at 16/24/48 kHz | `docs/ai/L1/02_architecture.md:75` |
| The aarch64 transport is Agora's RTSA SDK | `ai_agents/agents/scripts/package_agora_rtc_sdk_arm64.sh`, pinned `4.4.32-141` |
| `np.interp` resampling in-repo | `conversation_recorder/audio_mixer.py:38`, `qwen3_tts_python/qwen3_tts.py:204` |
| TEN API server port is `SERVER_PORT` | `ai_agents/server/main.go:92` |
| `run_llm_demo.sh` has no port flag | Table 4-3 of the Cooper development kit guide |
| Both daemons can be co-resident | `app_demo/test_alternate.py`, vendor-run |
| Simultaneous inference | **Not verified** — see §9 |
| `asr_d` file-mode minimum length | **Not verified** — see §9 |
