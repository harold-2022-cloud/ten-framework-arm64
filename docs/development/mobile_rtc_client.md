# Driving a TEN agent from a mobile app over Agora RTC

How to start a voice agent on a TEN device and talk to it from a phone, with no
web playground involved. Written for someone implementing the mobile client:
every claim below is anchored to the source that produces the behaviour.

Paths in this document are repo-relative, with four shorthands:

- `src/...` is inside the `agora_rtc` extension package (the wrapper source,
  not tracked in this repo — see [`arm64_build.md`](./arm64_build.md)).
- `main_python/...` is
  `ai_agents/agents/examples/voice-assistant/tenapp/ten_packages/extension/main_python/`.
- `message_collector2/...` is
  `ai_agents/agents/ten_packages/extension/message_collector2/`.
- `playground/...` is `ai_agents/playground/`.

## What connects to what

RTC is mediated by Agora's cloud. **The phone never connects to the device
directly.** Both join the same Agora channel as peers, and audio is exchanged
through Agora. The device's HTTP server is a control plane only — it exists to
put the agent into a channel and take it out again.

```
   phone ─────────┐
                  ├──→ Agora cloud (one channel) ──→ audio flows here
   device agent ──┘

   phone ──HTTP (LAN)──→ device:8080/start   ← control only, no media
```

Two consequences worth internalising before writing code:

- The phone needs the Agora RTC SDK, an App ID, a channel name and a token.
  It does not need to know the device's IP for *media*.
- The phone needs the device's IP only to call `/start` and `/stop`. If you
  would rather drive those from your own backend, the phone never has to reach
  the device at all.

## Prerequisites on the device

1. `agora_rtc` built for the target architecture and installed into the tenapp.
   On aarch64 see [`arm64_build.md`](./arm64_build.md).
2. `AGORA_APP_ID` set in `ai_agents/.env` (plus `AGORA_APP_CERTIFICATE` if the
   Agora project has certificates enabled — see [Tokens](#tokens)).
3. The stack running:

   ```bash
   cd ai_agents/agents/examples/voice-assistant && task run
   ```

   This starts the Go API server on 8080, the playground on 3000 and TMAN
   Designer on 49483. Only 8080 matters here.

The Go server binds every interface (`http_server.go:900`, `r.Run(":"+Port)`),
so a phone on the same LAN can reach it.

**8080 is the default, not a guarantee.** It comes from `SERVER_PORT` in
`ai_agents/.env`, and one deployment has to move it: see [Running the board's
own LLM](#running-the-boards-own-llm). Read the port out of `.env` rather than
hardcoding it.

## The HTTP API

Routes are registered in `ai_agents/server/internal/http_server.go:884-893`.
Every response uses the same envelope (`http_server.go:505-511`):

```json
{ "code": "0", "msg": "success", "data": { } }
```

`code` is a **string**, not a number. `"0"` is success; error codes are listed
in `ai_agents/server/internal/code.go:10-27` and reproduced under
[Error codes](#error-codes).

### POST /start — put the agent into a channel

Request fields are the `StartReq` struct, `http_server.go:50-61`:

```json
{
  "request_id": "6f1b0c9e-...",
  "channel_name": "myroom",
  "graph_name": "voice_assistant_soniox",
  "user_uid": 12345,
  "bot_uid": 999,
  "timeout": -1,
  "properties": {}
}
```

| Field | JSON name | Meaning |
| --- | --- | --- |
| `RequestId` | `request_id` | Echoed into server logs. Use a UUID; it is how you correlate a failure with its log lines. |
| `ChannelName` | `channel_name` | Agora channel. Also the key the server stores the worker under. |
| `GraphName` | `graph_name` | Which `predefined_graphs[]` entry to run. |
| `RemoteStreamId` | `user_uid` | The phone's uid. |
| `BotStreamId` | `bot_uid` | The agent's uid in the channel. |
| `Token` | `token` | Normally omit — the server mints its own (see [Tokens](#tokens)). |
| `QuitTimeoutSeconds` | `timeout` | Seconds of silence before the worker is killed. `-1` disables the timeout. |
| `Properties` | `properties` | Per-node property overrides, `{"<node name>": {"<property>": value}}`. |
| `TenappDir` | `tenapp_dir` | **Ignored.** The server always uses its launch directory (`http_server.go:60`). |

`request_id` and `properties` may be omitted.

**How the fields reach the graph.** `config.go:36-55` defines `startPropMap`,
and `http_server.go:679-702` walks it, matching on the graph **node name**
(`http_server.go:696`, `nodeMap["name"] == prop.ExtensionName`, where
`extensionNameAgoraRTC = "agora_rtc"`, `config.go:17`):

| Request field | Node | Property set |
| --- | --- | --- |
| `channel_name` | `agora_rtc`, `agora_rtm` | `channel` |
| `user_uid` | `agora_rtc` | `remote_stream_id` |
| `bot_uid` | `agora_rtc` | `stream_id` |
| `token` | `agora_rtc`, `agora_rtm` | `token` |
| `worker_http_server_port` | `http_server` | `listen_port` |

So a graph picks these up **only if its RTC node is named `agora_rtc`**. A node
addoned to `agora_rtc` but *named* something else is silently skipped.

`properties` is applied the same way (`http_server.go:644-656`) and merges
recursively (`mergeProperties`, `http_server.go:514`), so you can override a
single vendor parameter without restating the rest:

```json
"properties": { "tts": { "params": { "voice_id": "..." } } }
```

**Rejections, before any worker starts:**

- `workers.Size() >= WorkersMax` → `10001` / HTTP 429 (`http_server.go:255`)
- a worker already exists for this channel → `10003` / HTTP 400
  (`http_server.go:261`). One channel, one worker.
- empty `channel_name` → `10004` / HTTP 400

### POST /token/generate — credentials for the phone

```json
{ "channel_name": "myroom", "uid": 12345 }
```

Returns (`http_server.go:375` and `:387`):

```json
{ "code": "0", "msg": "success",
  "data": { "appId": "...", "token": "...", "channel_name": "myroom", "uid": 12345 } }
```

### POST /ping — keep the worker alive

```json
{ "channel_name": "myroom" }
```

Refreshes the worker's `UpdateTs`. Returns `10002` if no worker holds that
channel — which is also the normal answer before `/start` and after `/stop`, so
do not treat it as fatal. See [Keeping the worker alive](#keeping-the-worker-alive).

### POST /stop

```json
{ "channel_name": "myroom" }
```

Terminates the worker. The server sends SIGTERM and escalates to SIGKILL if the
process has not exited in time.

### Others

`GET /health`, `GET /list` (running workers), `GET /graphs` (name + auto_start
for each predefined graph — useful for populating a picker in your app).

## Tokens

`handlerGenerateToken` (`http_server.go:374-387`) and the `/start` path
(`http_server.go:596-600`) behave identically:

```go
req.Token = s.config.AppId                  // no certificate: the App ID stands in
if s.config.AppCertificate != "" {          // certificate set: sign a real RTM token
    req.Token, err = rtctokenbuilder.BuildTokenWithRtm(...)
}
```

**Set both `AGORA_APP_ID` and `AGORA_APP_CERTIFICATE`, or neither, matching the
Agora project.** If the project enforces certificates but `.env` leaves
`AGORA_APP_CERTIFICATE` empty, both peers present the App ID as their token,
Agora rejects the join, and — see [Silent failures](#silent-failures) — nothing
reports an error. You get a running graph and no audio.

Tokens are per-uid. The phone must request a token for the uid it will join
with.

## Mobile client sequence

```
1.  POST /start            channel_name, graph_name, user_uid, bot_uid, timeout
2.  POST /token/generate   channel_name, uid = user_uid   →  appId, token
3.  RtcEngine.joinChannel(token, channel_name, uid = user_uid)
4.  publish the microphone track
5.  subscribe to remote audio and play it   ← this is the agent's TTS
6.  POST /ping every < timeout seconds       (skip entirely if timeout = -1)
7.  POST /stop             channel_name
```

Steps 3-5 are ordinary Agora SDK usage on Android, iOS, Flutter or React
Native. There is no TEN-specific signalling, codec or framing to implement: the
agent publishes a normal audio track.

Start the agent (step 1) **before** or concurrently with joining. The agent
auto-joins during `on_start`, so it will already be in the channel — or will
arrive within a second or two — when the phone joins.

### Audio format

The device side of this pipeline is 16 kHz mono PCM16 end to end:

- Inbound default is `subscribe_audio_sample_rate{16000}`,
  `subscribe_audio_num_of_channels{1}` (`src/configs/predefined_props.h:56-57`).
- Outbound passes the frame's own rate straight through to the SDK
  (`src/rtc_connection.cc:423-425`) and rejects anything that is not 16-bit
  (`:404-406`).

The phone does not need to match this. The Agora SDK negotiates and resamples.

### Receiving transcripts

Transcripts arrive as RTC data-stream messages, in a chunked format that has to
be reassembled. See [Transcripts over the data
stream](#transcripts-over-the-data-stream).

## Transcripts over the data stream

If the graph has a `message_collector` node wired to `agora_rtc` — the
`voice_assistant*` graphs do — every ASR partial and every LLM delta is
published over the RTC data stream. This is how you render live captions.

Requires `publish_data: true` on the `agora_rtc` node; the default is `false`
(`src/configs/predefined_props.h:74`).

### The pipeline

```
main_python._send_transcript()                 main_python/extension.py:145-157
   │   data "message" → the message_collector node
   ▼
message_collector2.on_data()                   message_collector2/extension.py:53-60
   │   JSON → UTF-8 bytes → base64 → split into chunks
   ▼
_process_queue()                               message_collector2/extension.py:82-86
   │   Data.create("data"), set_property_buf("data", ...), one chunk per 40 ms
   ▼
agora_rtc.on_data()                            src/rtc_extension.cc:148
   │   get_property_buf("data")
   ▼
sendStreamMessage()                            src/rtc_connection.cc:1286
   ▼
your onStreamMessage handler
```

### Wire format

Each data-stream message is one chunk, formatted at
`message_collector2/helper.py:53`:

```
<msg_id>|<part_index>|<total_parts>|<base64 fragment>
```

| Field | Meaning |
| --- | --- |
| `msg_id` | 8 characters, `str(uuid.uuid4())[:8]` (`message_collector2/extension.py:57`) |
| `part_index` | **1-based**, not 0-based |
| `total_parts` | how many chunks this message was split into |
| base64 fragment | **a slice of one base64 string — not independently decodable** |

The whole formatted chunk is capped at 1024 bytes
(`message_collector2/helper.py:12` and the check at `:56`). The payload slice
is 924 characters in practice: 1024 minus the prefix, arrived at by the
decrement loop at `:60`.

### Parsing

```
1. bytes → ASCII string
2. split on "|" into 4 fields   (base64's alphabet has no "|", so this is safe)
3. cache each chunk under its msg_id
4. once total_parts chunks have arrived:
       sort by part_index
       concatenate the base64 fragments
5. base64-decode the concatenated string → UTF-8 → parse as JSON
```

**Concatenate before decoding.** 924 is not a multiple of 4, so decoding any
individual fragment produces garbage. The reference implementation is
`playground/src/manager/rtc/rtc.ts:292-298`:

```js
reconstructMessage(chunks) {
  chunks.sort((a, b) => a.part_index - b.part_index);   // order first
  return chunks.map((chunk) => chunk.content).join(""); // then join
}
```

Decoding then goes through bytes, not through a string
(`playground/src/manager/rtc/rtc.ts:300-307`): `atob` → `Uint8Array` →
`TextDecoder("utf-8")`. Treating the `atob` output as text directly mangles any
non-ASCII content.

### Payload schema

The decoded JSON, from `main_python/extension.py:145-157`:

```json
{
  "data_type": "transcribe",
  "role": "user",
  "text": "...",
  "text_ts": 1789310499000,
  "is_final": false,
  "stream_id": 149966
}
```

| Field | Meaning |
| --- | --- |
| `data_type` | `"transcribe"` — `text` is the text. `"raw"` — `text` is *itself* a JSON string, `{type, data}`, where `type` is `reasoning`, `image_url` or `action` (`main_python/extension.py:159-178`, handled at `rtc.ts:256-277`) |
| `role` | `"user"` or `"assistant"` — who spoke |
| `text` | the transcript |
| `text_ts` | milliseconds since epoch |
| `is_final` | `false` while the utterance is still being revised |
| `stream_id` | the speaker's RTC uid |

### Merging partials

**Every ASR partial and every LLM delta is its own message.** One conversation
turn easily produces dozens. Appending each one gives you `哈`, `哈喽`,
`哈喽，`, `哈喽，你` scrolling up the screen.

The playground merges them in `playground/src/store/reducers/global.ts:97-141`.
Grouped by `stream_id`:

```
find the last final item and the last non-final item for this stream_id

if a final item exists and incoming.time <= that item's time  →  discard
else if a non-final item exists                               →  replace it in place
else                                                          →  append a new item
```

Partials from one speaker overwrite each other until one arrives with
`is_final: true`, which becomes the anchor for the next group.

### Pitfalls

| Pitfall | Handling |
| --- | --- |
| Chunks can arrive out of order | Sort by `part_index`; never rely on arrival order |
| A message may never complete | Expire the cache (`rtc.ts:223-230`). Chunks are paced 40 ms apart (`message_collector2/extension.py:86`), so the timeout must exceed `total_parts × 40 ms` |
| `"???"` placeholder for `total_parts` | Substituted before send (`helper.py:71-74`), so it should never reach you — but `rtc.ts:203` still guards for it, and so should you |
| base64 decoded as a string | Decode to bytes, then UTF-8. Anything else corrupts non-ASCII text |

The full working parser is `playground/src/manager/rtc/rtc.ts:174-307`.

## Running the board's own LLM

The Ambarella AI Developer Kit (N1-655) ships an on-board LLM demo server, and
`voice_assistant_soniox_ambarella_llm` routes the `llm` node to it instead of
to OpenAI. Everything else in that graph — transport, ASR, TTS, connections —
is identical to `voice_assistant_soniox`.

Two things change for the client, and nothing else. Agora credentials, channel
naming, joining, audio and transcript parsing are all unaffected: swapping the
provider only touches the board.

### 1. The API server moves off 8080

The board's LLM demo server listens on 8080 and cannot be moved:
`run_llm_demo.sh` takes `ip`, `run_mode`, `model_type`, `bsize`, `model_path`
and `max_user`, and no port option. `ai_agents/.env` also defaults
`SERVER_PORT` to 8080, and the Go server binds `0.0.0.0`, which takes the port
on loopback too — so whichever starts second fails to bind, silently leaving
`/start` unreachable.

Move the TEN server, not the board's:

```
SERVER_PORT=8081
AGENT_SERVER_URL=http://localhost:8081
```

Every call the client makes — `/start`, `/token/generate`, `/ping`, `/stop` —
goes to the new port. Confirm with:

```bash
ss -lntp | grep -E ':3000|:808[0-9]|:49483'
```

`api` on 8081, `test_llm` on 8080, `next-server` on 3000, `tman` on 49483.

### 2. The graph name

```json
{ "graph_name": "voice_assistant_soniox_ambarella_llm" }
```

### One session at a time, held for 180 seconds

`run_llm_demo.sh` is documented with `--max_user 1`. The board treats each
distinct `Session-Id` as a distinct user, allows exactly one, and keeps a used
session for **180 seconds** after its last request before freeing it. Exceeding
that logs

```
[ERR] current user num (2) > max_user_num (1), please wait 180s ...
[ERR] handle_request_from_post fail
```

to `/tmp/log.txt` and closes the connection **with no HTTP response at all**,
which surfaces client-side as an empty reply rather than an error.

The extension generates one id per instance and reuses it across turns, so a
running agent stays within the budget. The trap is restarting: a fresh worker
draws a new id while the previous session is still held, and every inference
fails until it expires. Either wait it out, or pin `session_id` on the `llm`
node so restarts reuse one session.

`Session-Id` must be a **non-zero decimal integer** — the board parses it
numerically, and a non-numeric value becomes zero and is refused the same
silent way.

### Reasoning in the reply

`deepseek_7B` is an R1 distill and reasons before answering. A **non-streaming**
reply arrives as

```
<reasoning>
</think>

<answer>
```

— the closing tag with no opening one, because generation begins inside the
block. **Streaming replies carry no reasoning at all**, and the extension
streams by default, so this does not normally reach TTS; the extension strips
it on the non-streaming path regardless.

Streaming is SSE with one character per event and a bare-text payload, not
JSON:

```
data: 您

data: 好

```

The graph pins `response_format` to `sse` rather than sniffing it.

### Bringing the board's LLM up

```bash
cd /usr/share/ambarella/llm_demo/
./run_llm_demo.sh --run_mode start --model_type 9 \
    --model_path ~/demo_resources/llm_demo --ip 127.0.0.1 --max_user 1
```

The first load after boot takes up to 80 s; the board is ready once
`/tmp/log.txt` shows `Device ENABLE`. The LLM demo and the LLaVA demo cannot
run at the same time — they share a library that does not support it.

`ai_agents/agents/scripts/diagnose_ambarella_llm.sh` checks all of the above
without changing anything: ports, backend processes, model readiness, and a
ladder of requests that each vary one thing, reporting what the server logged
for each.

## Keeping the worker alive

`worker_common.go:148-159`:

```go
if worker.QuitTimeoutSeconds == WORKER_TIMEOUT_INFINITY { continue }
if worker.UpdateTs + int64(worker.QuitTimeoutSeconds) < nowTs { /* kill */ }
```

The default is 60 seconds (`worker_common.go:65`) and `UpdateTs` only advances
on `/ping`. Two viable designs:

- **Ping.** Call `POST /ping` every 20-30 s while the call is up. Gives you
  automatic cleanup if the phone dies mid-call.
- **Disable it.** Pass `"timeout": -1` at `/start`
  (`WORKER_TIMEOUT_INFINITY = -1`, `config.go:26`). Simpler, but a worker then
  survives until an explicit `/stop`, so make sure you always issue one.

## Verified behaviours

Things that are easy to assume wrongly. Each was read out of the source.

**The phone's uid does not have to equal `user_uid` for audio to reach the
agent.** With `subscribe_audio: true` the wrapper calls `subscribeAllAudio()`
(`src/rtc_connection.cc:1390`), subscribing to every remote user in the
channel, filtered only by an optional blocklist (`:1414-1419`).
`remote_stream_id` is used as PullParams metadata (`src/rtc_extension.cc:571`),
not as a subscription filter. Matching them is still recommended so that
transcript `stream_id` values are meaningful.

**`channel` in `property.json` is a placeholder.** `/start` overwrites it. The
value committed in the graph is never what a session uses.

**One channel, one worker.** Concurrent sessions need distinct `channel_name`
values.

**These three flags default to `false`**: `subscribe_audio`
(`src/configs/predefined_props.h:55`), `publish_audio` (`:64`) and
`publish_data` (`:74`). A graph that omits them loads cleanly and moves no
audio. The shipped `voice_assistant*` graphs set all three.

## Silent failures

**Join failure does not fail the graph.** `src/rtc_extension.cc:48-54`:

```cpp
if (!rc) {
    AGORA_RTC_EXT_LOGE("failed to auto join channel");
    // AGORA_RTC_ASSERT(0, "failed to auto join channel");   ← commented out
}
// NOTE: always on_start_done to make sure extension able to receive on_stop
ten.on_start_done();
```

Both asserts are commented out and `on_start_done()` runs unconditionally. A
bad App ID or token produces a healthy-looking worker with no audio. "No error"
is not evidence of a connection.

**An unrecognised command returns success.** `src/rtc_extension.cc:114-118`
answers anything it does not handle with `TEN_STATUS_CODE_OK` and
`detail: "not supported"`. A misspelled command name is never reported.

Because of both, verify against the log, not against the absence of errors.

## Verifying a connection

The worker writes its own log file, separate from the terminal. `/ping` and
`/start` responses carry the path:

```
LogFile:/tmp/ten_agent/app-<channel>-<timestamp>.log
```

```bash
grep -nE 'key_point.*config:|onConnected:|onUserJoined:|onUserAudioTrackSubscribed|onAudioTrackPublishSuccess|onFirstRemoteAudio' \
  $(ls -t /tmp/ten_agent/app-*.log | head -1)
```

| Line | Proves | Source |
| --- | --- | --- |
| `key_point ... config:` | the resolved app_id/channel — confirms `.env` was read | `src/rtc_extension.cc:31` |
| `onConnected:` | the agent is in the channel | `src/observer/connection_observer.cc:69` |
| `onUserJoined:` | the phone arrived | `:285` |
| `onUserAudioTrackSubscribed` | subscribed to the phone's mic | `src/observer/local_user_observer.cc:87` |
| `onFirstRemoteAudioFrame` | remote audio is actually arriving | `:408` |
| `onFirstRemoteAudioDecoded` | it decoded and entered the graph | `:419` |
| `onAudioTrackPublishSuccess` | the agent's TTS track is published | `:81` |

Failure side:

```bash
grep -nE 'onConnectionFailure:|onConnectionLost:|token expired|onAudioTrackPublicationFailure|onError: error' \
  $(ls -t /tmp/ten_agent/app-*.log | head -1)
```

`onConnectionFailure:` (`connection_observer.cc:266`) carries a `reason` code;
`token expired!!` (`:232`) means the project enforces certificates and
`AGORA_APP_CERTIFICATE` is unset or wrong.

Where the ladder stops tells you what is broken:

| Last line seen | Look at |
| --- | --- |
| nothing | `.env`, or the graph has no `agora_rtc` node named `agora_rtc` |
| `config:` only | App ID or token — the join never completed |
| `onConnected:` | channel name mismatch between the two peers |
| `onUserJoined:` | the phone is not publishing — mic permission, or it never published the track |
| `onFirstRemoteAudioDecoded` but no `asr_result` | RTC is fine; the problem is the ASR vendor |

## Try it without writing an app

Start an agent:

```bash
curl -X POST http://<device-ip>:8080/start \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"t1","channel_name":"myroom","graph_name":"voice_assistant_soniox","user_uid":12345,"bot_uid":999,"timeout":-1}'
```

Join `myroom` from Agora's own demo app (search "Agora Video Call" in the App
Store or Play Store) using the same App ID, and talk. Then:

```bash
curl -X POST http://<device-ip>:8080/stop \
  -H 'Content-Type: application/json' -d '{"channel_name":"myroom"}'
```

This exercises the whole path before any client code exists.

## Error codes

From `ai_agents/server/internal/code.go:10-27`. Values are strings.

| Code | Meaning |
| --- | --- |
| `0` | success |
| `10000` | params invalid |
| `10001` | workers limit reached |
| `10002` | channel not existed |
| `10003` | channel existed |
| `10004` | channel empty |
| `10005` | generate token failed |
| `10007` | parse json failed |
| `10100` | process property json failed |
| `10101` | start worker failed |
| `10102` | stop worker failed |
| `10104` | update worker failed |

## Related

- [`arm64_build.md`](./arm64_build.md) — building `agora_rtc` for aarch64
- `ai_agents/server/internal/http_server.go` — the API implementation
- `ai_agents/agents/examples/voice-assistant/tenapp/property.json` — the graphs
- `ai_agents/agents/scripts/diagnose_ambarella_llm.sh` — on-board LLM diagnostic
