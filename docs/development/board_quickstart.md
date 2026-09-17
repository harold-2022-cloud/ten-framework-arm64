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
repo and this repo cannot install it:

```bash
tools/ambarella/check_llm_board.sh
```

That compares the board against the developer kit's own guide — the launcher,
the two processes, the model files, the log, the ports — and reports
differences rather than fixing them. Every line should say `MATCHES` before you
go further.

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

Open the playground on port 3000 and pick a graph.

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
