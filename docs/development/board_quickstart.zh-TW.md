# 在安霸 N1-655 開發板上跑語音助理

從 clone 到能對話。語音跑在板子的 CPU 上，LLM（Qwen3.5-4B）跑在板子的
NPU 上，所以預設的 graph 完全不需要任何雲端語音服務。

英文版是 [`board_quickstart.md`](board_quickstart.md)。更長的背景——為什麼把語音
從 Vector Processor 搬到 CPU、量到什麼、擴充怎麼運作——在
[`cpu_speech_on_ambarella.md`](cpu_speech_on_ambarella.md)；arm64 的編譯本身在
[`arm64_build.md`](arm64_build.md)。

## 開始之前

板子的 LLM 是 vLLM（cooper-vllm）上的 Qwen3.5-4B，跑在板子的 NPU 上。先安裝一次；
腳本照開發套件手冊的步驟做，需要套件裡的哪些東西寫在它的檔頭：

```bash
tools/ambarella/install_qwen35_vllm.sh --model-tar ~/qwen3.5-4b_n1655-video.tar.gz
```

然後啟動它，並照 graph 的方式問一題：

```bash
tools/ambarella/serve_qwen35.sh start
tools/ambarella/serve_qwen35.sh test
```

`start` 會先停掉正在跑的原廠 DeepSeek 7B daemon（兩者不能共用 NPU），再載入
Qwen3.5-4B，大約一分鐘。裝好開機服務之後（見[或是：開機就常駐](#或是開機就常駐)），
它開機就會啟動，這些都不用再打。

原廠的 7B daemon（`/usr/share/ambarella/llm_demo/run_llm_demo.sh`）留在板子上，給
LLM 比較和退回時用：`serve_qwen35.sh stop` 會切過去，`tools/ambarella/check_llm_board.sh`
會拿它跟手冊比對。

## 安裝

```bash
git clone -b feat/arm64-native-build <這個 repo> ~/ten-framework
cd ~/ten-framework
ai_agents/agents/scripts/install_board_arm64.sh --dry-run   # 先看計畫
ai_agents/agents/scripts/install_board_arm64.sh
```

會問一次 `sudo`，只用在 `uv pip install --system` 那一步。每個階段都是冪等的，
失敗重跑不用手動收拾。加 `--run` 會順便啟動；`--help` 列出其他選項。

腳本會在沒有 `ai_agents/.env` 時建一份。**它不會填任何憑證。**

## 唯一必填的一項

```bash
$EDITOR ai_agents/.env
```

| 變數 | 必填？ | 填錯會怎樣 |
| --- | --- | --- |
| `AGORA_APP_ID` | **要** | API server 會檢查它**正好 32 個字元**，不是就 `os.Exit(1)`。結果是沒人在聽那個 port，playground 報 `ECONNREFUSED`。 |
| `AGORA_APP_CERTIFICATE` | 不用 | 除非你的 Agora 專案啟用了憑證，否則**留空是正確的**。留空時 server 會把 app id 當作 token 回傳，那正是 app-id-only 專案預期的行為。 |

對 `voice_assistant_sherpa_full` 來說就這樣而已。這個 graph 讀到的其他每一個
placeholder 都有 `|` 預設值：

| 變數 | 預設值 |
| --- | --- |
| `QWEN_LLM_BASE_URL` | `http://127.0.0.1:8000/v1` |
| `SHERPA_ONNX_ASR_MODEL_DIR` | `/home/lychee/zipformer_asr/models/...`，絕對路徑，見[模型](#模型) |
| `SHERPA_ONNX_TTS_VOICE_DIR` | `/home/lychee/piper_tts/vits/...`，絕對路徑，見[模型](#模型) |
| `WEATHERAPI_API_KEY` | 空字串——只有天氣工具會不能用 |

**沒有 `|` 預設值的 placeholder 不是可選的**：worker 會在解析 property 時直接退出，
graph 根本不會載入。

## 模型

安裝腳本會把兩個模型抓下來放在這裡：

| | 位置 |
| --- | --- |
| Zipformer ASR，中英雙語 | `$HOME/zipformer_asr/models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20` |
| Piper 中文語音 | `$HOME/piper_tts/vits/vits-piper-zh_CN-huayan-medium` |

`ASR_ROOT` 和 `TTS_ROOT` 可以換根目錄，`VOICES=en` 或 `VOICES=zh_en` 可以抓別的
語音——這些傳給 `ai_agents/agents/scripts/setup_cpu_asr_tts_arm64.sh`，也就是安裝
腳本內部呼叫的那一支。

**注意：graph 裡的預設值是絕對路徑，而且寫死 `/home/lychee`。** 那是在一台使用者
叫 `lychee` 的板子上寫的。如果你的使用者不是，或你搬了根目錄，預設值就指到空的，
擴充會直接報出它找不到哪個目錄。在 `.env` 裡設定：

```bash
SHERPA_ONNX_ASR_MODEL_DIR=/home/<你的使用者>/zipformer_asr/models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20
SHERPA_ONNX_TTS_VOICE_DIR=/home/<你的使用者>/piper_tts/vits/vits-piper-zh_CN-huayan-medium
```

### 資料夾名稱不重要

兩個擴充都不看資料夾叫什麼，而是在裡面 glob，所以**任何名稱、任何位置都可以**，
只要內容對：

| | 裡面必須要有 | 有的話會用 |
| --- | --- | --- |
| `SHERPA_ONNX_ASR_MODEL_DIR` | `encoder-*.onnx`、`decoder-*.onnx`、`joiner-*.onnx`、`tokens.txt` | 有 `int8` 變體的話優先於 float 版 |
| `SHERPA_ONNX_TTS_VOICE_DIR` | 任何 `*.onnx`、`tokens.txt` | `espeak-ng-data/`、`lexicon*.txt` |

所以解壓成別的名字完全沒問題，把變數指到裝著那些檔的目錄就好。少了其中一項的話，
**啟動時**就會報出缺的是什麼，不會拖到第一句話才爆。

## Port

| Port | 誰在用 | 說明 |
| --- | --- | --- |
| 8000 | vLLM 上的 Qwen3.5-4B，板子上的 graph 都問它 | `serve_qwen35.sh` 用 `QWEN_PORT`；graph 讀 `QWEN_LLM_BASE_URL` |
| 8080 | 原廠的 DeepSeek 7B daemon（切換過去時） | `run_llm_demo.sh` 沒有 port 選項，所以這個動不了 |
| 8081 | 這個 repo 的 Go API server | `SERVER_PORT`，由安裝腳本從 8080 移開 |
| 3000 | playground | |
| 49483 | TMAN Designer | |

`.env.example` 裡 `SERVER_PORT` 預設是 8080，而在這塊板子上那個 port 已經被佔了。
如果 playground 報 `Parse Error: Invalid header token`，代表它正打進 LLM——回來的是
LLM 的錯誤頁，它的第二行 header 是一個沒有冒號的 `LLM`，不是合法的 header。
把 `SERVER_PORT` 和 `AGENT_SERVER_URL` 移開；8080 留給 7B daemon，板子切換到 7B
時它就在那裡。

## 啟動

```bash
cd ai_agents/agents/examples/voice-assistant
task run 2>&1 | tee /tmp/task_run.log
```

打開 3000 port 的 playground，選一個 graph。不透過 playground 直接呼叫的話，
見 [`agent_api.zh-TW.md`](agent_api.zh-TW.md)。

### 或是：開機就常駐

會議記錄 App 要求板子隨時都能回應手機。用平常跑 `task run` 的帳號執行一次
（sudo 會要你的密碼）：

```bash
tools/ambarella/install_board_services.sh
```

它會裝三個 systemd 服務。`qwen35-vllm`（vLLM 上的 Qwen3.5-4B，port 8000，板子上的
graph 都問它）和 `ten-api`（`task run-api-server`，log 寫到 `/tmp/task_run.log`）設成
開機啟動並立刻啟動，掛掉都會自動重起；`ten-api` 等 Qwen3.5-4B 能回應才啟動，大約是
開機後一分鐘。`ambarella-llm`（原廠的 DeepSeek 7B daemon，參數同上，log 一樣寫到
`/tmp/log.txt`）只安裝、開機不啟動。之後重開機什麼都不用做：手機一發起請求，server
就會起會議 worker。原本手動起的 server 會被停掉、改由服務接手；要用 playground 的話，
在 `ai_agents/agents/examples/voice-assistant` 跑 `task run-frontend`。`--status` 看
各自有沒有起來，以及開機後 Qwen3.5-4B 何時能回應、`ten-api` 何時啟動；`--uninstall`
移除三個服務。每次 `git pull` 之後都再跑一次。

### 板子的 LLM

2026 年 10 月起，板子的 LLM 從 DeepSeek 7B 換成 Qwen3.5-4B（cooper-vllm，用
`tools/ambarella/install_qwen35_vllm.sh` 安裝）：在驗收會議上，它沒有捏造分工，只有
2 處小錯，7B 則有 8 處。兩者不能同時跑，用 `tools/ambarella/serve_qwen35.sh` 切換：

| 指令 | 做什麼 |
| --- | --- |
| `serve_qwen35.sh status` | 現在是哪一個；vLLM 是否用腳本現在的參數在跑 |
| `serve_qwen35.sh test`、`test-tools` | 照 graph 的方式問一題；帶天氣工具問一題 |
| `serve_qwen35.sh restart` | 模板或參數改了之後，重新啟動 Qwen3.5-4B |
| `serve_qwen35.sh stop` | 改由 7B 回應，直到 `start` 或下次開機 |
| `serve_qwen35.sh start` | 改回 Qwen3.5-4B |

要長期退回 7B：先用 `tools/ambarella/install_board_services.sh --uninstall` 移除服務，
再 checkout 切換前的那個 commit，從那裡安裝——那個版本的腳本會把 7B 和 API server
設成開機啟動：

```bash
git checkout "$(git log --format=%h -1 --grep 'run Qwen3.5-4B as a service')^"
tools/ambarella/install_board_services.sh
```

之後只要再跑一次現在的腳本，就會切回 Qwen3.5-4B。

天氣 graph 只有在 `ai_agents/.env` 設了 `WEATHERAPI_API_KEY` 時才會呼叫工具；沒設的話
就不用工具直接回答。

手機上的會議記錄 App 怎麼用，從設定到分享記錄：
[`ai_agents/meeting-app-android/README.zh-TW.md`](../../ai_agents/meeting-app-android/README.zh-TW.md)。

## 選哪個 graph

| Graph | ASR | TTS | LLM | 需要 |
| --- | --- | --- | --- | --- |
| **`voice_assistant_sherpa_full`** | sherpa-onnx，CPU | sherpa-onnx，CPU | 板子，Qwen3.5-4B | **只要 `AGORA_APP_ID`** |
| `voice_assistant_sherpa_tts` | soniox，雲端 | sherpa-onnx，CPU | 板子，Qwen3.5-4B | `SONIOX_ASR_API_KEY` |
| `voice_assistant_soniox_ambarella_llm` | soniox，雲端 | elevenlabs，雲端 | 板子，Qwen3.5-4B | `SONIOX_ASR_API_KEY`、`ELEVENLABS_TTS_KEY` |
| 其餘十個 | 雲端 | 雲端 | OpenAI 或 Anthropic | 各自的金鑰；都不使用板子 |

這份文件講的是 `voice_assistant_sherpa_full`。另外兩個的存在是為了在同一塊板子上
把雲端元件和本地元件拿來對比。

**新增或移除 graph 不能熱更新**——前端會快取 `/graphs`，所以 server 和 playground
都要重啟。改既有 graph 裡的值則是下一個 session 生效。

## 會議記錄 graph

`meeting_minutes` 是 voice-assistant tenapp 的第 14 張 graph，和 13 張對話 graph
並存——同一套部署，`POST /start` 帶 `graph_name` 切換場景。會議在手機上完整錄成
一個 Ogg-Opus 檔（16 kHz 單聲道），散會後一次 HTTP 上傳。板子把它切成話題、逐話題
分說話人並轉文字、讓同一個聲音在整場都是同一個編號、用板上的 LLM 逐話題摘要，
最後在板子上留下原始音訊、`record.json` 和 `minutes.txt`。處理時間約和會議一樣長：板上 38 分鐘的驗證會議 35 分鐘做完。

（原本這裡的串流範例 `examples/meeting-minutes` 已刪除，代碼停在 `67085cfef`。）

### 安裝

```bash
tools/ambarella/install_meeting_models.sh        # 四個模型
ai_agents/agents/scripts/install_board_arm64.sh  # pull 之後要再跑一次
```

安裝器要重跑，是因為這張 graph 帶進了四個擴充——`meeting_uploader`、
`meeting_segmenter`、`meeting_transcriber`、`meeting_control_python`——它們的
Python 套件（`soundfile`、`aiohttp`、`sherpa-onnx`）必須裝進 runtime 用的 3.12，
而不是 shell 的 3.13。裝完要重啟：新增 graph 不能熱更新。

`ai_agents/.env` 裡視情況設：

| 變數 | 預設 | 什麼時候要設 |
| --- | --- | --- |
| `DIARIZATION_SEG_MODEL`、`DIARIZATION_EMB_MODEL`、`SENSEVOICE_MODEL_DIR`、`MEETING_VAD_MODEL` | `/home/lychee` 底下 | 板子的使用者不是 `lychee`；`install_meeting_models.sh` 最後會印出路徑 |
| `MEETINGS_DIR` | `/tmp/meetings` | 板子的 `/tmp` 是 tmpfs——歸檔會佔記憶體、重開機就不見 |
| `MEETING_AUTH_TOKEN` | 空，不檢查 | 設了之後，8765 的每個請求都要帶 `Authorization: Bearer <token>` |
| `MEETING_OUTPUT_SCRIPT` | `simplified` | 上傳沒指定 `script` 時，記錄用繁體（`traditional`）還是簡體 |

### 在板子上驗證

```bash
python3.12 tools/ambarella/check_meeting_board.py --check
python3.12 tools/ambarella/check_meeting_board.py \
  --audio ~/meeting_probe/M_R003S01C01.wav \
  --rttm ~/meeting_probe/M_R003S01C01.rttm --speakers 6 --minutes 12
```

第一行只檢查不動手：server 列得出這張 graph、LLM daemon 有回應、模型都在、
3.12 import 得到需要的套件、存會議的地方不是 tmpfs 且空間夠、8765 沒被佔。
第二行像客戶端一樣跑一場會：用 `run_meeting_speaker_probe.sh` 之前放到板子上的
AISHELL-4 錄音，截 12 分鐘讓它跨兩個話題；報告話題數、人數、摘要、時間、
worker 記憶體，並對照標註算出「說話時間算錯人」的比例。它**故意用 `timeout` 60 秒
啟動**：處理遠超過 60 秒，worker 撐到最後還在回應，就證明它自己保活成功。
結果存在 `~/meeting_probe/board_check`。

多場會議能不能共用同一個 worker（Android App 靠的就是這個）：對 `meeting-room`
第二次 `/start` 會回 `10003`；一場會議處理中時另一個會議 worker 起來，不會動到
那場；閒置的 worker 會被回收；重新起一個還讀得到那場會議。花的時間約是上傳的
錄音長度（4 分鐘）再加兩分鐘：

```bash
python3.12 tools/ambarella/check_meeting_worker.py
```

之前跑到一半卡住的話，用 `tools/ambarella/rerun_meeting_worker_check.sh`：它會先清掉
留下來的東西（還在跑的檢查、會議 worker），補裝會議 extension 缺的套件，再跑檢查。

`verify_meeting_board.sh --phone` 最後會等 Android App 傳一場會議過來，在板子上跟著它
跑完，檢查記錄、字體、App 用的 channel，以及 App 有沒有讓 worker 留著。

### 使用

```bash
BOARD=192.168.1.50
curl -s -X POST http://$BOARD:8081/start -H 'Content-Type: application/json' \
  -d '{"request_id":"1","channel_name":"meeting-room","graph_name":"meeting_minutes","timeout":600}'
until curl -sf http://$BOARD:8765/meetings > /dev/null; do sleep 1; done
curl -s -X POST http://$BOARD:8765/meeting/upload \
  -F 'file=@meeting.ogg' -F 'speakers=6' -F 'title=週會'
curl -s http://$BOARD:8765/meeting/<meeting_id>        # 狀態、進度
curl -s -O http://$BOARD:8765/meeting/<meeting_id>/minutes.txt
```

上傳完客戶端就可以離線：處理期間 worker 自己 ping server，所以 `timeout` 只要
涵蓋上傳和取回結果。一次只處理一場：處理中再上傳會排隊（`"status": "queued"`，附
`ahead`、`wait_s`），前一場做完就接著處理。

channel 一律用 `meeting-room`（Android App 也是），板子上就永遠只有一個會議
worker：再打一次 `/start` 會回 code `10003`（已經在跑），照樣上傳就好。不要
`/stop` 它，它可能正在處理別人的會議；最後一場結束 10 分鐘後板子會自己收掉。

### 演示大屏

板子自己的 HDMI 螢幕可以跟著每一場會議顯示給觀眾看：一個開機就啟動的服務（`meeting-screen`，跟其他服務一起安裝），加上桌面一啟動就全螢幕開的頁面：

```bash
tools/ambarella/install_board_services.sh --kiosk on
tools/ambarella/prepare_meeting_demo.sh
```

運作方式、按鍵和演示當天的流程：[`ai_agents/meeting-screen/README.zh-TW.md`](../../ai_agents/meeting-screen/README.zh-TW.md)。

### 已經量過的

結構化待辦那件事已經量過了，結論是這顆 7B 做不到——六輪數據和探測腳本在：

```bash
python3 tools/ambarella/probe_structured_minutes.py --prompts quoted
```

順帶量到兩個對任何 graph 都適用的事實：**板子的 LLM 是決定性的**（同輸入同輸出
到 byte，所以「跑十次看穩不穩」驗不出東西，要驗只能換輸入），以及**一次 LLM
turn 實際要 23–135 秒**，視輸出長度而定。

這兩件事都是換成 Qwen3.5-4B 之前、在 DeepSeek 7B 上量的。Qwen3.5-4B 處理 38 分鐘
的會議時，5 段話題每段約 20 秒，結論約 80 秒。

## 本地語音模型：動手依賴這些數字之前，先量一遍

分段門檻訂 30 秒安不安全，看兩個數字：diarization 處理一個收掉
的話題要多久，SenseVoice 處理同一段音訊要多久。兩個加起來，就是處理進度
落後會議本身多少——預設值要舒服地大於這個數字，不然一個話題還沒處理完，
下一個就已經收掉了。

```bash
python3 tools/ambarella/probe_diarization.py --audio /tmp/four.wav \
  --speakers 4 --threads 4
```

（如果已經跑過 `install_meeting_models.sh`，diarization 模型已經在
`~/diarization_models` 底下了，這裡直接用，不用再 `--fetch`。）
`--threads 4` 對應的是這個 graph 裡 `meeting_transcriber` 的屬性
（`tenapp/property.json` 的 `num_threads: 4`）——跟語音助理自己 CPU 語音那
邊用幾個執行緒，是不相干的另一個設定。拿它報出來的即時率，去跟兩個執行緒
時已經量過的數字比：**0.49**。

SenseVoice 在這塊板子上跑多快，目前**沒量過**——還沒有專門的探測腳本。就用
`meeting_transcriber` 自己載入模型的方式，手動對同一段音訊計時一次：

```python
import time
import sherpa_onnx

recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model="path/to/sensevoice/model.onnx",   # 排序後排第一的那個 model*.onnx
    tokens="path/to/sensevoice/tokens.txt",
    num_threads=4,
    use_itn=True,
    provider="cpu",
)
samples = ...  # float32，範圍 [-1, 1]——跟 probe_diarization.py 讀進去的同一段音訊
stream = recognizer.create_stream()
stream.accept_waveform(16000, samples)
t0 = time.monotonic()
recognizer.decode_stream(stream)
print(f"{time.monotonic() - t0:.2f}s  ->  {stream.result.text}")
```

把兩個耗時加起來，就是一個話題要花多久處理完。乘上一場會的話題數，就是散會
之後要等多久——新設計是批次的，所以這個數字決定的是等待時間，不再像舊設計那樣
決定「處理追不追得上錄音」。

## 對話之後

```bash
tools/ambarella/check_asr_log.sh
```

它讀 `/tmp/task_run.log`，回報 ASR 這條路徑的行為：每句話是被引擎自己的 endpoint
收掉的還是被備援計時器收掉的、尾靜音多長、解碼器有沒有跟上。每一項檢查都對應一個
真的發生過的故障，所以通過代表那一個沒有再回來。

要對真模型跑測試，而且是在 **runtime 實際載入的直譯器**下跑，不是 `PATH` 上隨便
哪個 `python3`：

```bash
tools/ambarella/verify_asr_board.sh
```

## 出問題的時候

| 你看到的 | 那是什麼 |
| --- | --- |
| `Dependency resolution failed without specific error details` | `tman` 解不出 `agora_rtc`——registry 沒有 arm64 build，而 manifest 把它釘死。安裝腳本會在解析時把它拿掉、之後再放入預編譯版；**在這裡不能用 `task install`**。 |
| `Parse Error: Invalid header token` | playground 打進了 8080 的 LLM。把 `SERVER_PORT` 移開。 |
| API server 的 port 出現 `ECONNREFUSED` | 要嘛 `server/bin/api` 從來沒被建出來，要嘛 `AGORA_APP_ID` 不是 32 個字元、server 已經退出。看 `/tmp/task_run.log`。 |
| 擴充報 `ModuleNotFoundError` | 套件裝到了 shell 的 `python3`，不是 runtime 載入的那個。`tools/ambarella/setup_tts_runtime_deps.sh --check` 會報出哪個直譯器有什麼。 |
| 板子回得很慢 | 那個模型是推理蒸餾版，會先把推理過程寫完才給答案。實測 167～373 個字，速度 5～10 字／秒，整段等待就是這個。**介面上沒有任何可以關掉它的設定。** |

`ai_agents/.env`、`ai_agents/server/bin/`、各個 `tenapp/bin/` **全部是 git-ignore
的**，所以「我這份能跑」完全不能證明重新 clone 下來能跑。改了安裝流程要驗證的話，
**clone 到一個新目錄**，不要在已經能跑的那份上 `git pull`。
