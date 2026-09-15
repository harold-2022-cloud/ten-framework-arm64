# agora_rtc, prebuilt for linux/arm64

The TEN registry publishes `agora_rtc` and `agora_rtc_sdk` for linux/x64 only,
so an arm64 board otherwise has to build the wrapper from source and repackage
the SDK before it can join an Agora channel. These are the results of doing
that once, on an Ambarella N1-655 (aarch64 Fedora), so that nobody else has to.

---

## Running an agent with RTC on an arm64 board

**Status:** each step below has been run on the board, but not in this order
from a clean clone. Treat it as a recipe that has not had its final rehearsal.

### 0. Prerequisites

An aarch64 Linux board with `task`, `tman`, `go`, `bun` and Python 3.10+.
`tman` comes from the framework's own build; see
[`arm64_build.md`](../../../../docs/development/arm64_build.md) if it is not
on PATH yet.

### 1. Clone and install the example

```bash
git clone git@github.com:harold-2022-cloud/ten-framework-arm64.git
cd ten-framework-arm64
git checkout feat/arm64-native-build

cd ai_agents/agents/examples/voice-assistant
task install
```

`task install` pulls the Python extensions, the Go server and the playground.
It does **not** bring `agora_rtc` — that is the next step, and the reason this
directory exists.

### 2. Install the prebuilt RTC extension

```bash
cd ../../../..          # back to the repo root
ai_agents/agents/scripts/install_prebuilt_agora_rtc_arm64.sh voice-assistant
```

It refuses to run anywhere but aarch64, checks that no x86-64 object ended up
under any `lib/`, and resolves every symbol in `libagora_rtc.so` against the
installed `ten_runtime` before reporting success. A failure here is a real
failure, not a warning.

### 3. Fill in `ai_agents/.env`

Copy `ai_agents/.env.example` if you have no `.env` yet. The
`voice_assistant_soniox` graph reads:

| Variable | Needed |
| --- | --- |
| `AGORA_APP_ID` | yes |
| `AGORA_APP_CERTIFICATE` | only if your Agora project enables tokens |
| `SONIOX_ASR_API_KEY` | yes |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | yes |
| `ELEVENLABS_TTS_KEY` | yes |
| `WEATHERAPI_API_KEY` | optional, for the weather tool |

Set `AGORA_APP_CERTIFICATE` and `AGORA_APP_ID` together or not at all, matching
the Agora project. A project that enforces certificates and a `.env` that
leaves the certificate empty makes both peers present the App ID as their
token; Agora refuses the join and **nothing reports an error** — the graph
loads and no audio flows.

### 4. Run

```bash
cd ai_agents/agents/examples/voice-assistant
task run 2>&1 | tee /tmp/task_run.log
```

Three services start: the Go API server on 8080, the playground on 3000, TMAN
Designer on 49483.

### 5. Open the playground

The browser only ever talks to port 3000; the other two are reached through a
server-side proxy. If the browser is on another machine, forward that one port
rather than browsing to the board's address:

```bash
ssh -N -L 3000:localhost:3000 <user>@<board>
```

then open `http://localhost:3000`. **`localhost` is required**, not the board's
IP: `getUserMedia` is only available in a secure context, so an insecure origin
has no microphone at all.

Pick a graph whose transport is `agora_rtc` — `voice_assistant_soniox` is the
one these steps configure — and press Connect.

### 6. Confirm audio actually arrived

A joined channel is not proof: `RtcExtension::on_start` logs a failed join and
calls `on_start_done()` anyway, so a misconfigured App ID yields a healthy
looking graph and silence. The evidence is in the log:

```bash
grep -nE 'onConnected:|onUserJoined:|onFirstRemoteAudioFrame|onAudioTrackPublishSuccess' /tmp/task_run.log
```

`onFirstRemoteAudioFrame` is the first line that proves remote audio reached
the board. [`mobile_rtc_client.md`](../../../../docs/development/mobile_rtc_client.md)
has the full ladder and what each rung failing means.

---

## Using the board's own LLM instead of OpenAI

`voice_assistant_soniox_ambarella_llm` routes the `llm` node to the Ambarella
LLM demo server. Two things change:

1. **Move the TEN API server off 8080.** The demo server holds that port and
   `run_llm_demo.sh` has no option to move it, so set `SERVER_PORT=8081` and
   `AGENT_SERVER_URL=http://localhost:8081` in `ai_agents/.env`.
2. Start the demo server first, and wait for `Device ENABLE` in `/tmp/log.txt`
   — the first model load after boot takes up to 80 s.

`ai_agents/agents/scripts/diagnose_ambarella_llm.sh` checks all of that without
changing anything.

Do **not** put ASR and TTS on the board as well. Measured on an N1-655 on
2026-09-14, the three models share one Vector Processor and it does not run
them in parallel: ASR under a generating LLM went from 348 ms to 24 s, and one
LLM turn took 24 minutes. `tools/ambarella/vp_concurrency_probe.py` reproduces
the measurement.

---

## Rebuilding

To build from source — a different SDK version, a patched wrapper — use
`setup_agora_rtc_arm64.sh`, then `export_agora_rtc_arm64.sh` to refresh this
directory from what it produced.

## What is here

| | |
| --- | --- |
| `agora_rtc/` | the extension: `lib/libagora_rtc.so` plus its manifest and property |
| `agora_rtc_sdk/` | the five SDK libraries the wrapper links against |

Runtime files only. The SDK's 154 headers are needed to compile the wrapper,
never to run it, and they were most of the size.

The libraries are stripped. They load and resolve exactly as before — the
dynamic symbol tables are untouched, only the debug symbols are gone — but a
backtrace from a crash inside them will show addresses rather than function
names. Rebuild if you need to debug one.

## Versions

| Package | Version |
| --- | --- |
| `agora_rtc` | `0.23.9-t1` |
| `agora_rtc_sdk` | `4.4.32-141` |

`agora_rtc`'s manifest declares both x64 and arm64 under `supports`, because
that is the upstream manifest with arm64 added. The binaries here are aarch64
only, which is why the install script refuses to run anywhere else.

The two are pinned to each other: `0.26` calls `setTotalExtraSendMs` and needs
SDK `4.4.32-175`, so they cannot be upgraded independently.
