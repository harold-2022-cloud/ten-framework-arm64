# Running the voice assistant on an Ambarella N1-655 board

From a clone to a conversation. Speech runs on the board's CPU and the LLM is
the vendor's daemon, so nothing in the default graph talks to a cloud speech
provider.

A Traditional Chinese edition of this page is at
[`board_quickstart.zh-TW.md`](board_quickstart.zh-TW.md).

Longer background — why speech was moved off the Vector Processor, what was
measured, and how the extensions work — is in
[`cpu_speech_on_ambarella.md`](cpu_speech_on_ambarella.md). The arm64 build
itself is in [`arm64_build.md`](arm64_build.md).

## Before you start

The board needs the vendor's LLM demo already running. It is not part of this
repo and this repo cannot install it. Start it the way the developer kit's
guide says:

```bash
cd /usr/share/ambarella/llm_demo/
./run_llm_demo.sh --run_mode start --model_type 9 \
    --model_path ~/demo_resources/llm_demo --ip 127.0.0.1 --max_user 1
```

**Run it from that directory.** Both processes inherit the launcher's working
directory, and that is what puts the log at `/tmp/log.txt`. Started from
somewhere else, the log lands there instead and every tool that reads it —
including `check_llm_board.sh` — finds nothing.

The first load after a boot takes up to about 80 seconds. It is ready when
`/tmp/log.txt` contains a `Device ENABLE` line. A healthy board then shows
exactly two processes, `test_llm` and `test_llm_client`; more than that means
an earlier run is still holding a session.

`--max_user 1` is one conversation at a time. A second request while one is
still generating is refused, and `/tmp/log.txt` says so:
`current user num (2) > max_user_num (1), please wait 180s`.

Then check all of it:

```bash
tools/ambarella/check_llm_board.sh
```

That compares the board against the guide — the launcher, the two processes,
the model files, the log, the ports — and reports differences rather than
fixing them. Every line should say `MATCHES` before you go further.

## Install

```bash
git clone -b feat/arm64-native-build <this-repo> ~/ten-framework
cd ~/ten-framework
ai_agents/agents/scripts/install_board_arm64.sh --dry-run   # read the plan
ai_agents/agents/scripts/install_board_arm64.sh
```

It asks for `sudo` once, for the `uv pip install --system` step only. Every
stage is idempotent, so a failed run can be restarted without undoing anything.
`--run` also starts the app; `--help` lists the rest.

The script writes `ai_agents/.env` if there is none. **It fills in no
credentials.**

## The one thing you must fill in

```bash
$EDITOR ai_agents/.env
```

| Variable | Needed? | What happens if it is wrong |
| --- | --- | --- |
| `AGORA_APP_ID` | **yes** | The API server checks it is exactly 32 characters and calls `os.Exit(1)` if not. Nothing listens, and the playground reports `ECONNREFUSED`. |
| `AGORA_APP_CERTIFICATE` | no | Leave it empty unless your Agora project has certificates enabled. Empty means the server hands the app id back as the token, which is what an app-id-only project expects. |

That is the whole list for `voice_assistant_sherpa_full`. Everything else the
graph reads has a `|` default in `property.json`:

| Variable | Default |
| --- | --- |
| `AMBARELLA_LLM_BASE_URL` | `http://127.0.0.1:8080` |
| `SHERPA_ONNX_ASR_MODEL_DIR` | `/home/lychee/zipformer_asr/models/...` — absolute, see [The models](#the-models) |
| `SHERPA_ONNX_TTS_VOICE_DIR` | `/home/lychee/piper_tts/vits/...` — absolute, see [The models](#the-models) |
| `WEATHERAPI_API_KEY` | empty — only the weather tool stops working |

A placeholder without a `|` default is not optional: the worker exits during
property resolution and the graph never loads.

## The models

The install script fetches both and leaves them here:

| | Where |
| --- | --- |
| Zipformer ASR, bilingual zh-en | `$HOME/zipformer_asr/models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20` |
| Piper voice, zh | `$HOME/piper_tts/vits/vits-piper-zh_CN-huayan-medium` |

`ASR_ROOT` and `TTS_ROOT` move those roots, and `VOICES=en` or `VOICES=zh_en`
fetches a different voice — pass them to
`ai_agents/agents/scripts/setup_cpu_asr_tts_arm64.sh`, which the install script
calls.

**The graph's defaults are absolute and say `/home/lychee`.** They were written
on a board whose user is `lychee`. If yours is not, or you moved the roots, the
defaults point at nothing and the extension reports the directory it could not
find. Set them in `.env`:

```bash
SHERPA_ONNX_ASR_MODEL_DIR=/home/<you>/zipformer_asr/models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20
SHERPA_ONNX_TTS_VOICE_DIR=/home/<you>/piper_tts/vits/vits-piper-zh_CN-huayan-medium
```

### The directory name does not matter

Neither extension looks at what the folder is called. Each one globs inside it,
so any name and any location work as long as the contents do:

| | Must contain | Also used if present |
| --- | --- | --- |
| `SHERPA_ONNX_ASR_MODEL_DIR` | `encoder-*.onnx`, `decoder-*.onnx`, `joiner-*.onnx`, `tokens.txt` | an `int8` variant of each is preferred over the float one |
| `SHERPA_ONNX_TTS_VOICE_DIR` | any `*.onnx`, `tokens.txt` | `espeak-ng-data/`, `lexicon*.txt` |

So a bundle unpacked under a different name is fine; point the variable at the
directory that holds those files. A directory that is missing one of them fails
at start with the name of what it could not find, rather than at the first
spoken word.

## Ports

| Port | Who | Note |
| --- | --- | --- |
| 8080 | the vendor's LLM daemon | `run_llm_demo.sh` has no port option, so this one cannot move |
| 8081 | this repo's Go API server | `SERVER_PORT`, moved off 8080 by the install script |
| 3000 | the playground | |
| 49483 | TMAN Designer | |

`SERVER_PORT` defaults to 8080 in `.env.example`, which on this board is
already taken. If the playground reports `Parse Error: Invalid header token`,
it is proxying into the LLM: what comes back is an LLM error page whose second
header line is the bare word `LLM`, which is not a valid header. Move
`SERVER_PORT` and `AGENT_SERVER_URL`, and leave `AMBARELLA_LLM_BASE_URL` on
8080 where the daemon actually is.

## Run

```bash
cd ai_agents/agents/examples/voice-assistant
task run 2>&1 | tee /tmp/task_run.log
```

Open the playground on port 3000 and pick a graph. Driving it without the
playground is [`agent_api.md`](agent_api.md).

### Or keep it running from power-on

For the meeting-minutes app, the board has to answer whenever a phone asks.
Once, as the user who runs `task run` (sudo asks for your password):

```bash
tools/ambarella/install_board_services.sh
```

It installs two systemd services, enables them at boot and starts them:
`ambarella-llm` (the vendor's daemon above, same options, still logging to
`/tmp/log.txt`) and `ten-api` (`task run-api-server`, restarted if it dies,
logging to `/tmp/task_run.log`). Nothing else is needed after a reboot: the
server starts the meeting worker when a phone asks. A server already started
by hand is stopped so the service can take over; for the playground, run
`task run-frontend` in `ai_agents/agents/examples/voice-assistant`.
`--status` says what is up, `--uninstall` removes both.

## Which graph

| Graph | ASR | TTS | LLM | Needs |
| --- | --- | --- | --- | --- |
| **`voice_assistant_sherpa_full`** | sherpa-onnx, CPU | sherpa-onnx, CPU | the board | **nothing but `AGORA_APP_ID`** |
| `voice_assistant_sherpa_tts` | soniox, cloud | sherpa-onnx, CPU | the board | `SONIOX_ASR_API_KEY` |
| `voice_assistant_soniox_ambarella_llm` | soniox, cloud | elevenlabs, cloud | the board | `SONIOX_ASR_API_KEY`, `ELEVENLABS_TTS_KEY` |
| the other ten | cloud | cloud | OpenAI or Anthropic | their own keys; none of them uses the board |

`voice_assistant_sherpa_full` is the one this quickstart is about. The other two
exist to compare a cloud component against a local one on the same board.

Adding or removing a graph is not hot-reloadable — the frontend caches
`/graphs`, so the server and the playground both need restarting. Editing
values inside an existing graph takes effect on the next session.

## The meeting graph

`meeting_minutes` is the voice-assistant tenapp's 14th graph, beside the 13
conversation graphs — one deployment, `POST /start` with a `graph_name` to
pick the scenario. A meeting is recorded whole on a phone as one Ogg-Opus
file (16 kHz mono) and uploaded over HTTP once it ends. The board cuts it
into topics, tells the speakers apart and transcribes each topic, gives
every voice one number across the meeting, summarises each topic with its
own LLM, and keeps the audio, `record.json` and `minutes.txt` on the board.
Processing takes about as long as the meeting: 35 minutes for the
38-minute check meeting on the board.

(The streaming example that used to be here, `examples/meeting-minutes`, is
deleted; its code stops at `67085cfef`.)

### Set it up

```bash
tools/ambarella/install_meeting_models.sh        # the four models
ai_agents/agents/scripts/install_board_arm64.sh  # again, after pulling
```

The installer has to run again because the graph brought four extensions —
`meeting_uploader`, `meeting_segmenter`, `meeting_transcriber`,
`meeting_control_python` — and their Python packages (`soundfile`,
`aiohttp`, `sherpa-onnx`) must land in the runtime's 3.12, not the shell's
3.13. Then restart: adding a graph is not hot-reloadable.

In `ai_agents/.env`, if they apply:

| Variable | Default | When to set it |
| --- | --- | --- |
| `DIARIZATION_SEG_MODEL`, `DIARIZATION_EMB_MODEL`, `SENSEVOICE_MODEL_DIR`, `MEETING_VAD_MODEL` | under `/home/lychee` | the board's user is not `lychee`; `install_meeting_models.sh` prints the paths |
| `MEETINGS_DIR` | `/tmp/meetings` | `/tmp` is tmpfs on the board — archives would live in RAM and vanish on reboot |
| `MEETING_AUTH_TOKEN` | empty, no check | every request to 8765 must then carry `Authorization: Bearer <token>` |
| `MEETING_OUTPUT_SCRIPT` | `simplified` | the record's script when an upload does not choose one (`script=traditional` or `simplified`) |

### Check it on the board

```bash
python3.12 tools/ambarella/check_meeting_board.py --check
python3.12 tools/ambarella/check_meeting_board.py \
  --audio ~/meeting_probe/M_R003S01C01.wav \
  --rttm ~/meeting_probe/M_R003S01C01.rttm --speakers 6 --minutes 12
```

The first only looks: the server lists the graph, the LLM daemon answers,
the models exist, the packages import under 3.12, where meetings land is
not tmpfs and has room, nothing holds 8765. The second runs a meeting the
way a client would — the AISHELL-4 recording `run_meeting_speaker_probe.sh`
already put on the board, cut to 12 minutes so it spans two topics — and
reports topics, speakers, summaries, time, the worker's memory and, from
the reference, the share of speech given to the wrong person (2.3% for
these 12 minutes in the dev container). It starts the graph with `timeout` 60 on purpose:
processing takes far longer, so a worker that is still answering at the
end proves it kept itself alive. Results go to `~/meeting_probe/board_check`.

Whether meetings can share the one worker, as the Android app needs — a
second `/start` on `meeting-room` answers `10003`, a second meeting worker
started while a meeting is processed leaves that meeting alone, the idle
worker is reaped, and a fresh one still reads the meeting. About the length
of the audio it uploads (4 minutes) plus two:

```bash
python3.12 tools/ambarella/check_meeting_worker.py
```

If an earlier run got stuck, `tools/ambarella/rerun_meeting_worker_check.sh`
clears what it left (a check still running, meeting workers), installs any
package the meeting extensions cannot import, and then runs the check.

`verify_meeting_board.sh --phone` ends by waiting for a meeting from the
Android app: it follows it on the board and checks the record, its script,
the app's channel, and that the app left the worker running.

### Use it

```bash
BOARD=192.168.1.50
curl -s -X POST http://$BOARD:8081/start -H 'Content-Type: application/json' \
  -d '{"request_id":"1","channel_name":"meeting-room","graph_name":"meeting_minutes","timeout":600}'
until curl -sf http://$BOARD:8765/meetings > /dev/null; do sleep 1; done
curl -s -X POST http://$BOARD:8765/meeting/upload \
  -F 'file=@meeting.ogg' -F 'speakers=6' -F 'title=weekly'
curl -s http://$BOARD:8765/meeting/<meeting_id>        # state, progress
curl -s -O http://$BOARD:8765/meeting/<meeting_id>/minutes.txt
```

The client can go offline after the upload: while a meeting is being
processed the worker pings the server for itself, so `timeout` only has to
cover the upload and fetching the result. One meeting at a time — a second
upload while one is processing gets 409.

Use the channel `meeting-room`, as the Android app does, and there is only
ever one meeting worker: a second `/start` answers code `10003` (already
running), which is fine — upload to it. Don't `/stop` it, since it may be
in the middle of someone else's meeting. The board reaps it ten minutes
after its last meeting.

### Already measured

Whether the board's 7B can produce structured action items has been
measured, and the answer is no. Six rounds of evidence, and the probe:

```bash
python3 tools/ambarella/probe_structured_minutes.py --prompts quoted
```

It also measured two things that hold for any graph on this board: **the
LLM is deterministic** (same input, same output down to the byte, so
repeating a prompt measures nothing — vary the input instead), and **one LLM
turn really costs 23–135 seconds**, depending on how much it writes.

## Local speech models: measure before you rely on it

Two numbers decide whether a 30 s segmentation threshold is safe: how long
diarization takes on one topic, and how long SenseVoice takes on the
same audio. Together they are how far processing lags behind the meeting —
the default has to sit comfortably above that, or a topic closes before the
previous one has finished being worked on.

```bash
python3 tools/ambarella/probe_diarization.py --audio /tmp/four.wav \
  --speakers 4 --threads 4
```

(If `install_meeting_models.sh` already ran, the diarization models are
already under `~/diarization_models` and this just uses them — no `--fetch`
needed here.) `--threads 4` matches what `meeting_transcriber` will be
given — a separate setting from whatever thread count the voice assistant's
own CPU speech setup uses.
Compare the real-time factor it reports against the one already measured at
two threads: **0.49**.

SenseVoice's own speed on this board is **unmeasured** — there is no probe
script for it yet. Time one pass by hand, over the same audio, the way
`meeting_transcriber` itself loads the model:

```python
import time
import sherpa_onnx

recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model="path/to/sensevoice/model.onnx",   # whichever model*.onnx sorts first
    tokens="path/to/sensevoice/tokens.txt",
    num_threads=4,
    use_itn=True,
    provider="cpu",
)
samples = ...  # float32 in [-1, 1] — the same audio probe_diarization.py read
stream = recognizer.create_stream()
stream.accept_waveform(16000, samples)
t0 = time.monotonic()
recognizer.decode_stream(stream)
print(f"{time.monotonic() - t0:.2f}s  ->  {stream.result.text}")
```

Add the two elapsed times to get how long one topic takes to process.
Multiply by the topics in a meeting and that is how long you wait after it
ends — the new design is batch, so this number sets the wait rather than
deciding, as the old one did, whether processing can keep up with the
recording at all.

## After a conversation

```bash
tools/ambarella/check_asr_log.sh
```

It reads `/tmp/task_run.log` and reports whether the ASR path behaved: whether
each utterance was ended by the engine's own endpoint or by the stand-in timer,
how much trailing silence ended it, and whether the decoder kept up. Each check
is a failure that happened once, so a pass means that one did not come back.

To run the tests against the real model, under the interpreter the runtime
loads rather than whichever `python3` is on `PATH`:

```bash
tools/ambarella/verify_asr_board.sh
```

## When something is wrong

| What you see | What it is |
| --- | --- |
| `Dependency resolution failed without specific error details` | `tman` cannot resolve `agora_rtc` — the registry has no arm64 build and the manifest pins it exactly. The install script drops it for the resolve and puts the prebuilt one in afterwards; plain `task install` cannot work here. |
| `Parse Error: Invalid header token` | The playground is proxying into the LLM on 8080. Move `SERVER_PORT`. |
| `ECONNREFUSED` on the API server's port | Either `server/bin/api` was never built, or `AGORA_APP_ID` is not 32 characters and the server exited. Check `/tmp/task_run.log`. |
| `ModuleNotFoundError` from an extension | Packages went to the shell's `python3` rather than the one the runtime loads. `tools/ambarella/setup_tts_runtime_deps.sh --check` reports which interpreter has what. |
| The board answers slowly | The model is a reasoning distill and writes its reasoning before the answer. Measured: 167–373 characters of it at 5–10 characters a second, which is the whole of the wait. There is no interface setting for it. |

`ai_agents/.env`, `ai_agents/server/bin/` and each `tenapp/bin/` are all
git-ignored, so a working checkout proves nothing about a fresh clone. If you
change the install, verify it by cloning into a new directory rather than by
pulling into one that already works.
