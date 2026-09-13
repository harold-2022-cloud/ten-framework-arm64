# Driving a TEN agent from a mobile app over Agora RTC

How to start a voice agent on a TEN device and talk to it from a phone, with no
web playground involved. Written for someone implementing the mobile client:
every claim below is anchored to the source that produces the behaviour.

Paths in this document are repo-relative, except those beginning `src/`, which
are inside the `agora_rtc` extension package (the wrapper source, not tracked in
this repo — see [`arm64_build.md`](./arm64_build.md) for where it lives).

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

If the graph contains a `message_collector` node wired to `agora_rtc` (the
`voice_assistant*` graphs do), transcripts are published as RTC data-stream
messages: the wrapper opens a stream with `createDataStream`
(`src/rtc_connection.cc:946`) and writes to it with `sendStreamMessage`
(`:1286`). Implement `onStreamMessage` on the phone to render live captions.
Requires `publish_data: true` on the `agora_rtc` node — the default is `false`.

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
