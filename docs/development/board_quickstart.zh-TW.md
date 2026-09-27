# 在安霸 N1-655 開發板上跑語音助理

從 clone 到能對話。語音跑在板子的 CPU 上，LLM 是原廠的 daemon，所以預設的
graph 完全不需要任何雲端語音服務。

英文版是 [`board_quickstart.md`](board_quickstart.md)。更長的背景——為什麼把語音
從 Vector Processor 搬到 CPU、量到什麼、擴充怎麼運作——在
[`cpu_speech_on_ambarella.md`](cpu_speech_on_ambarella.md)；arm64 的編譯本身在
[`arm64_build.md`](arm64_build.md)。

## 開始之前

板子上要先有原廠的 LLM demo 在跑。那不是這個 repo 的一部分，這個 repo 也裝不了它。
照開發套件手冊的方式啟動：

```bash
cd /usr/share/ambarella/llm_demo/
./run_llm_demo.sh --run_mode start --model_type 9 \
    --model_path ~/demo_resources/llm_demo --ip 127.0.0.1 --max_user 1
```

**一定要從那個目錄執行。** 兩個程序都會繼承啟動器的工作目錄，那正是讓日誌落在
`/tmp/log.txt` 的原因。從別的地方啟動，日誌就會跑到那個地方去，所有讀它的工具
——包括 `check_llm_board.sh`——都會找不到。

開機後第一次載入模型最久約 80 秒。`/tmp/log.txt` 出現 `Device ENABLE` 那一行才算
就緒。這時健康的板子**正好兩個程序**：`test_llm` 和 `test_llm_client`；多於兩個
代表前一次的執行還佔著 session。

`--max_user 1` 是一次只能一段對話。上一段還在生成時送第二個請求會被拒絕，
`/tmp/log.txt` 會寫出來：
`current user num (2) > max_user_num (1), please wait 180s`。

然後把上面每一項都檢查一遍：

```bash
tools/ambarella/check_llm_board.sh
```

它拿板子的實際狀態去比對手冊——啟動器、兩個程序、模型檔、日誌、port——只回報
差異，不做任何修改。**每一行都要是 `MATCHES`** 才往下走。

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
| `AMBARELLA_LLM_BASE_URL` | `http://127.0.0.1:8080` |
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
| 8080 | 原廠的 LLM daemon | `run_llm_demo.sh` 沒有 port 選項，所以這個動不了 |
| 8081 | 這個 repo 的 Go API server | `SERVER_PORT`，由安裝腳本從 8080 移開 |
| 3000 | playground | |
| 49483 | TMAN Designer | |

`.env.example` 裡 `SERVER_PORT` 預設是 8080，而在這塊板子上那個 port 已經被佔了。
如果 playground 報 `Parse Error: Invalid header token`，代表它正打進 LLM——回來的是
LLM 的錯誤頁，它的第二行 header 是一個沒有冒號的 `LLM`，不是合法的 header。
把 `SERVER_PORT` 和 `AGENT_SERVER_URL` 移開，而 `AMBARELLA_LLM_BASE_URL`
**要維持 8080**，那是 daemon 真正的位置。

## 啟動

```bash
cd ai_agents/agents/examples/voice-assistant
task run 2>&1 | tee /tmp/task_run.log
```

打開 3000 port 的 playground，選一個 graph。不透過 playground 直接呼叫的話，
見 [`agent_api.zh-TW.md`](agent_api.zh-TW.md)。

## 選哪個 graph

| Graph | ASR | TTS | LLM | 需要 |
| --- | --- | --- | --- | --- |
| **`voice_assistant_sherpa_full`** | sherpa-onnx，CPU | sherpa-onnx，CPU | 板子 | **只要 `AGORA_APP_ID`** |
| `voice_assistant_sherpa_tts` | soniox，雲端 | sherpa-onnx，CPU | 板子 | `SONIOX_ASR_API_KEY` |
| `voice_assistant_soniox_ambarella_llm` | soniox，雲端 | elevenlabs，雲端 | 板子 | `SONIOX_ASR_API_KEY`、`ELEVENLABS_TTS_KEY` |
| 其餘十個 | 雲端 | 雲端 | OpenAI 或 Anthropic | 各自的金鑰；都不使用板子 |

這份文件講的是 `voice_assistant_sherpa_full`。另外兩個的存在是為了在同一塊板子上
把雲端元件和本地元件拿來對比。

**新增或移除 graph 不能熱更新**——前端會快取 `/graphs`，所以 server 和 playground
都要重啟。改既有 graph 裡的值則是下一個 session 生效。

## 會議記錄（meeting-minutes）graph

另一個範例，在 `ai_agents/agents/examples/meeting-minutes`，跑的是不同的
graph：`meeting_minutes`。它會錄下一場會議，隨著靜音把每個話題收掉、轉錄，
然後跟板子的 LLM 要一份摘要。沒有 playground，也不走 RTC。

```bash
tools/ambarella/install_meeting_models.sh    # 先抓兩組語音模型
cd ai_agents/agents/examples/meeting-minutes
task run 2>&1 | tee /tmp/task_run.log
```

`meeting_minutes` 的 `auto_start` 是 `false`，所以不會自己啟動——要用
`POST /start`、帶上 `"graph_name": "meeting_minutes"` 來啟動它，做法見
[`agent_api.zh-TW.md`](agent_api.zh-TW.md)。就算這個 graph 完全不碰 RTC，
`AGORA_APP_ID` 還是得填：Go server 在啟動時就會無條件檢查它，比看你要哪個
graph 還早。Go API server 本身的 8081 port，跟語音助理是同一回事——見上面的
[Port](#port)，這裡沒有任何不同。

### 音訊怎麼進來：走 WebSocket

這個範例沒有 playground。音訊是送進 `websocket_server` 這個擴充，不是走 RTC
也不是瀏覽器：port `8765`，監聽所有介面（`0.0.0.0`）——這些預設值在它自己的
`manifest.json` 和 `property.json` 裡，這個 graph 沒有覆蓋它們。也沒有路徑
（path）要對：這個 server 不按路徑分流。送 PCM16、單聲道、16 kHz、
base64 編碼，包成 JSON：

```json
{"audio": "<base64 PCM16 mono 16kHz>"}
```

協定就這樣，沒有別的了。沒有 handshake，沒有開始／結束訊息，會議結束時也
不用送什麼——不送音訊了就是了。

### 這個 graph 要的模型

| | 環境變數覆蓋 | 預設值 |
| --- | --- | --- |
| Diarization，segmentation | `DIARIZATION_SEG_MODEL` | `/home/lychee/diarization_models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx` |
| Diarization，embedding | `DIARIZATION_EMB_MODEL` | `/home/lychee/diarization_models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx` |
| SenseVoice ASR | `SENSEVOICE_MODEL_DIR` | `~/sensevoice` |

```bash
tools/ambarella/install_meeting_models.sh
```

會把三個都抓下來——diarization 那兩個是呼叫 `probe_diarization.py --fetch`，
SenseVoice 才是這支腳本自己的新工作——然後印出它解出來的路徑。跟語音助理的
模型一樣，上面的預設值假設板子的使用者是 `lychee`；如果不是，把印出來的路徑
填進 `.env`，變數名稱就用上面那三個。

錄音會放到 `/home/lychee/meeting_segments`：graph 啟動時要有 250 MB 空間
（只在那時候檢查一次，之後不會再盯著），之後大概每小時 115 MB。跟上面三個
模型路徑不一樣，這個路徑在 `property.json` 裡不是 `${env:...}` 覆蓋——換了
使用者的話，得直接改那個檔案，或者讓那個路徑本身存在。

### 會議記錄怎麼拿出來

沒有任何東西把記錄送到 UI——因為根本沒有 UI。完成的記錄會寫成
`/home/lychee/meeting_segments` 裡的 `meeting_record.json`，就在它描述的
那份錄音旁邊，裡面有 `started_at`、`transcript`、`meeting_summary`，還有
一個 `segments` 清單。

那個檔案什麼時候寫出來，由靜音決定：

| 屬性 | 預設值 | 作用 |
| --- | --- | --- |
| `segment_silence_s` | `30` | 把目前的話題收掉 |
| `min_segment_s` | `5` | 短於這個長度的段落不會獨立成段，會併進下一段 |
| `meeting_silence_s` | `600` | 結束整場會議，觸發彙整 |
| `speakers` | `3` | 一開始就告訴 diarizer，每一段都用 |

`speakers` 很重要：沒給人數的話，分群會把同一個人拆成好幾個——board 測試
量到的是，四個人被判成七個。

彙整是再跟板子的 LLM 講一次話，拿已經收集好的各話題摘要，去問整場會議的
結論。所以 `meeting_record.json` 會在最後一個人講完話之後過一段時間才出現，
不是 600 秒計時器一到就馬上有。

### 動手依賴這些數字之前，先量一遍

`segment_silence_s` 訂 30 秒安不安全，看兩個數字：diarization 處理一個收掉
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

把兩個耗時加起來，就是一個話題收掉之後要花多久處理完。如果這個數字沒有舒服
地小於 30 秒，代表 `segment_silence_s` 已經在啃下一個話題的處理時間，而不是
好好等它做完。

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
