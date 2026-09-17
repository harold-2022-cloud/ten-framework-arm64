# Agent API

`ai_agents/server` 裡的 Go server 是 playground 唯一會對話的對象。它是無狀態的
路由層：一個 channel 對應一個 worker 程序，用 HTTP 啟動和停止。

英文版是 [`agent_api.md`](agent_api.md)。

## 它聽哪個 port

`ai_agents/.env` 裡的 `SERVER_PORT`，板子的安裝腳本會設成 **8081**。預設是 8080，
而在板子上那個 port 屬於原廠的 LLM daemon——見
[`board_quickstart.zh-TW.md`](board_quickstart.zh-TW.md#port)。以下一律用 8081；
在沒有那個 daemon 的機器上是 8080。

```bash
curl -s http://127.0.0.1:8081/health
```

## 每個回應的形狀都一樣

```json
{ "code": "0", "msg": "success", "data": null }
```

`code` 是**字串**，`"0"` 才是成功。**HTTP 200 不代表呼叫成功**——要看 `code`。

| Code | 意思 |
| --- | --- |
| `0` | ok / success |
| `10000` | 參數無效 |
| `10001` | worker 數量到上限（`WORKERS_MAX`） |
| `10002` | channel 不存在 |
| `10003` | channel 已存在——一個 channel 只能有一個 worker，那個已經在跑 |
| `10004` | channel 是空的——`channel_name` 沒給或是空白 |
| `10005` | 產生 token 失敗 |
| `10006` | 存檔失敗 |
| `10007` | 解析 JSON 失敗 |
| `10100` | 處理 property json 失敗 |
| `10101` | 啟動 worker 失敗 |
| `10102` | 停止 worker 失敗 |
| `10103` | http status 不是 200 |
| `10104` | 更新 worker 失敗 |
| `10105` | 讀目錄失敗 |
| `10106` | 讀檔失敗 |

## 路由

| Method | Path | 做什麼 |
| --- | --- | --- |
| `GET` | `/`、`/health` | 存活檢查。只回 `code 0`。 |
| `GET` | `/graphs` | `tenapp/property.json` 裡的 graph，只有 name 和 `auto_start`。 |
| `GET` | `/list` | 現在正在跑的 worker。 |
| `POST` | `/token/generate` | 給某個 channel 和 uid 的 Agora RTC token。 |
| `POST` | `/start` | 在某個 channel 上用某個 graph 啟動 worker。 |
| `POST` | `/ping` | 讓那個 worker 繼續活著。 |
| `POST` | `/stop` | 停掉它。 |
| `GET` | `/dev-tmp/addons/default-properties` | 每個 addon 的預設 property 區塊。 |
| `GET` | `/vector/document/preset/list` | |
| `POST` | `/vector/document/update` | |
| `POST` | `/vector/document/upload` | |

## 一段完整的 session

### 1. 有哪些 graph

```bash
curl -s http://127.0.0.1:8081/graphs | jq '.data'
```

`/graphs` 讀的是 server 啟動時指定的那個 tenapp 的 `property.json`。所以**新增或
移除 graph 需要重啟 server**，而且 playground 還會在上面再快取一層。

### 2. 拿 token

```bash
curl -s -X POST http://127.0.0.1:8081/token/generate \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"1","channel_name":"demo","uid":12345}' | jq '.data'
```

```json
{ "appId": "...", "token": "...", "channel_name": "demo", "uid": 12345 }
```

`AGORA_APP_CERTIFICATE` 留空時，`token` 回來的就是 app id 本身。**那不是失敗**，
那正是 app-id-only 的 Agora 專案預期的行為。

### 3. 啟動

```bash
curl -s -X POST http://127.0.0.1:8081/start \
  -H 'Content-Type: application/json' \
  -d '{
        "request_id": "1",
        "channel_name": "demo",
        "graph_name": "voice_assistant_sherpa_full",
        "user_uid": 12345,
        "bot_uid": 54321,
        "token": "<第 2 步拿到的>",
        "timeout": 60
      }'
```

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `request_id` | string | 你自己給的，會出現在日誌裡。 |
| `channel_name` | string | **必填。** 這是 worker 的身分。同一個 channel 啟動兩次會得到 `10003`。 |
| `graph_name` | string | `/graphs` 列出來的名字。 |
| `user_uid` | uint32 | 使用者的 RTC uid。 |
| `bot_uid` | uint32 | agent 的 RTC uid。 |
| `token` | string | 第 2 步拿到的。 |
| `timeout` | int | 多少秒沒有 ping 就回收這個 worker。沒給就用 `WORKER_QUIT_TIMEOUT_SECONDS`。 |
| `properties` | object | 各節點的屬性覆寫，見下。 |
| `worker_http_server_port` | int32 | 通常不給。 |
| `tenapp_dir` | string | **會被忽略。** server 永遠用啟動時指定的那個目錄。 |

### 4. 讓它活著

```bash
curl -s -X POST http://127.0.0.1:8081/ping \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"2","channel_name":"demo"}'
```

每次 ping 會更新 worker 的時間戳。**最後一次時間戳超過它的 timeout 的 worker 會被
server 停掉**，所以 client 斷線不會留下殘留程序。收到 `10002` 代表它已經不在了。

### 5. 停止

```bash
curl -s -X POST http://127.0.0.1:8081/stop \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"3","channel_name":"demo"}'
```

## `/start` 會往 graph 裡寫什麼

`property.json` 裡的 graph 是範本。請求裡的四個欄位會在 worker 啟動前被注入進去，
所以**新的擴充只要宣告對應的 property 就會自動拿到，不用改 server**：

| 請求欄位 | 擴充 | Property |
| --- | --- | --- |
| `channel_name` | `agora_rtc`、`agora_rtm` | `channel` |
| `user_uid` | `agora_rtc` | `remote_stream_id` |
| `bot_uid` | `agora_rtc` | `stream_id` |
| `bot_uid` | `agora_rtm` | `user_id`，轉成字串 |
| `token` | `agora_rtc`、`agora_rtm` | `token` |

## 覆寫某個擴充的屬性

`properties` 是「節點名稱 → 要合併進那個節點的屬性」的 map，**只對這一次 session
有效**，磁碟上的 `property.json` 不會被動到。

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

key 是 graph 裡的**節點名稱**，不是 addon 名稱——`stt`、`tts`、`llm`、
`main_control`。值本身是物件的屬性（例如 `params`）會**遞迴合併**，所以只指定裡面
一個 key 不會動到其他的；值是純量的屬性則是**直接取代**。

這是不改檔案、不重啟 server 就能試不同 endpoint 規則或語速的方法。只對那一個
worker 有效。

## 其他

`WORKERS_MAX` 限制同時存在的 worker 數，超過時 `/start` 回 `10001`。這塊板子上的
LLM 是 `--max_user 1`，所以同時多於一段對話在更下面那層就會被拒絕了。

server 會注入 `graph_name` 但**不會驗證它**。不存在於 `property.json` 的名字要到
worker 啟動時才失敗，錯誤碼是 `10101`。

要看實際在跑什麼，`/list` 最快：

```bash
curl -s http://127.0.0.1:8081/list | jq '.data'
```
