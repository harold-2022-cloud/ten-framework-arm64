# The agent API

The Go server in `ai_agents/server` is the only thing the playground talks to.
It is stateless routing: one worker process per channel, started and stopped
over HTTP.

A Traditional Chinese edition of this page is at
[`agent_api.zh-TW.md`](agent_api.zh-TW.md).

## Where it listens

`SERVER_PORT` in `ai_agents/.env`, which the board's install sets to **8081**.
The default is 8080 and that belongs to the vendor's LLM daemon there — see
[`board_quickstart.md`](board_quickstart.md#ports). Everything below uses 8081;
on a machine without that daemon it is 8080.

```bash
curl -s http://127.0.0.1:8081/health
```

## Every response has the same shape

```json
{ "code": "0", "msg": "success", "data": null }
```

`code` is a string and `"0"` is success. HTTP 200 does not mean the call
worked — read `code`.

| Code | Means |
| --- | --- |
| `0` | ok / success |
| `10000` | params invalid |
| `10001` | workers limit — `WORKERS_MAX` reached |
| `10002` | channel not existed |
| `10003` | channel existed — one worker per channel, already running |
| `10004` | channel empty — `channel_name` missing or blank |
| `10005` | generate token failed |
| `10006` | save file failed |
| `10007` | parse json failed |
| `10100` | process property json failed |
| `10101` | start worker failed |
| `10102` | stop worker failed |
| `10103` | http status not 200 |
| `10104` | update worker failed |
| `10105` | read directory failed |
| `10106` | read file failed |

## Routes

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/` , `/health` | Liveness. Returns `code 0` and nothing else. |
| `GET` | `/graphs` | The graphs in `tenapp/property.json`, name and `auto_start` only. |
| `GET` | `/list` | The workers running now. |
| `POST` | `/token/generate` | An Agora RTC token for a channel and uid. |
| `POST` | `/start` | Start a worker on a channel with a graph. |
| `POST` | `/ping` | Keep that worker alive. |
| `POST` | `/stop` | Stop it. |
| `GET` | `/dev-tmp/addons/default-properties` | Each addon's default property block. |
| `GET` | `/vector/document/preset/list` | | 
| `POST` | `/vector/document/update` | |
| `POST` | `/vector/document/upload` | |

## A session, end to end

### 1. Which graphs are there

```bash
curl -s http://127.0.0.1:8081/graphs | jq '.data'
```

`/graphs` reads `property.json` from the tenapp the server was launched
against. Adding or removing a graph therefore needs the server restarted, and
the playground caches the list on top of that.

### 2. A token

```bash
curl -s -X POST http://127.0.0.1:8081/token/generate \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"1","channel_name":"demo","uid":12345}' | jq '.data'
```

```json
{ "appId": "...", "token": "...", "channel_name": "demo", "uid": 12345 }
```

With `AGORA_APP_CERTIFICATE` empty, `token` comes back as the app id itself.
That is not a failure: it is what an app-id-only Agora project expects.

### 3. Start

```bash
curl -s -X POST http://127.0.0.1:8081/start \
  -H 'Content-Type: application/json' \
  -d '{
        "request_id": "1",
        "channel_name": "demo",
        "graph_name": "voice_assistant_sherpa_full",
        "user_uid": 12345,
        "bot_uid": 54321,
        "token": "<from step 2>",
        "timeout": 60
      }'
```

| Field | Type | Notes |
| --- | --- | --- |
| `request_id` | string | Yours, echoed into the log. |
| `channel_name` | string | **Required.** The worker's identity. Starting twice on one channel returns `10003`. |
| `graph_name` | string | A name from `/graphs`. |
| `user_uid` | uint32 | The human's RTC uid. |
| `bot_uid` | uint32 | The agent's RTC uid. |
| `token` | string | From `/token/generate`. |
| `timeout` | int | Seconds of silence before the worker is reaped. Falls back to `WORKER_QUIT_TIMEOUT_SECONDS`. |
| `properties` | object | Per-extension overrides, below. |
| `worker_http_server_port` | int32 | Usually left out. |
| `tenapp_dir` | string | **Ignored.** The server always uses the directory it was launched with. |

### 4. Keep it alive

```bash
curl -s -X POST http://127.0.0.1:8081/ping \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"2","channel_name":"demo"}'
```

Every ping stamps the worker. A worker whose last stamp is older than its
timeout is stopped by the server, so a client that goes away does not leave a
process behind. `10002` means it is already gone.

### 5. Stop

```bash
curl -s -X POST http://127.0.0.1:8081/stop \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"3","channel_name":"demo"}'
```

## What `/start` writes into the graph

The graph in `property.json` is a template. Four fields of the request are
injected into it before the worker starts, so a new extension picks them up by
declaring the property — no server change:

| Request field | Extension | Property |
| --- | --- | --- |
| `channel_name` | `agora_rtc`, `agora_rtm` | `channel` |
| `user_uid` | `agora_rtc` | `remote_stream_id` |
| `bot_uid` | `agora_rtc` | `stream_id` |
| `bot_uid` | `agora_rtm` | `user_id`, as a string |
| `token` | `agora_rtc`, `agora_rtm` | `token` |

## Overriding an extension's properties

`properties` is a map of node name to the properties to merge into that node
for this session only. `property.json` on disk is not touched.

```bash
curl -s -X POST http://127.0.0.1:8081/start \
  -H 'Content-Type: application/json' \
  -d '{
        "request_id": "1",
        "channel_name": "demo",
        "graph_name": "voice_assistant_sherpa_full",
        "properties": {
          "stt": { "rule2_min_trailing_silence": 0.8 },
          "tts": { "speed": 1.1 }
        }
      }'
```

The keys are node names from the graph, not addon names — `stt`, `tts`, `llm`,
`main_control`. A property whose value is an object, such as `params`, is
merged recursively, so naming one key inside it leaves the rest alone. A
property whose value is a scalar is replaced.

This is the way to try a different endpoint rule or voice speed without
editing a file or restarting the server. It lasts for that worker only.

## Notes

`WORKERS_MAX` caps concurrent workers; beyond it `/start` returns `10001`. On
this board the LLM is `--max_user 1`, so more than one conversation at a time
is refused further down anyway.

The server injects, but does not validate, `graph_name`. A name that is not in
`property.json` fails when the worker starts, as `10101`.

`/list` is the quickest way to see what is actually running:

```bash
curl -s http://127.0.0.1:8081/list | jq '.data'
```
