# 需求：agora_rtc extension 的 aarch64 build

## 一、背景

TEN Framework 的 AI agents 已在 aarch64 Linux 上完整驗證可用，語音對話全鏈路
（麥克風 → ASR → LLM → TTS → 播放）原生跑通，無需模擬。

驗證平台：Lychee 2025 aarch64（Fedora rebuild），glibc 2.41，gcc 14.3.1。

唯一無法使用的是 **RTC 傳輸**。`ai_agents/agents/examples/` 底下 26 個 example，
其中 **24 個依賴 `agora_rtc` extension**，在 aarch64 上全部無法運行。
目前唯一可用的是 `websocket-example`（以 WebSocket 取代 RTC 作為傳輸層）。

## 二、需求：4 個 aarch64 二進位

### A. `agora_rtc` extension 套件（2 個，主要阻塞點）

| 檔案 | 版本 |
| --- | --- |
| `libagora_rtc.so` | 對齊 `0.23.9-t1` |
| `liblinux_audio_hy_extension.so` | 同上 |

目前發佈的 `agora_rtc` 套件只含四個檔案：`manifest.json`、`property.json`，
以及上述兩個 `.so` 的 x64 版本，**不含源碼**。

### B. `agora_rtc_sdk` 套件缺少的（2 個）

| 檔案 | 說明 |
| --- | --- |
| `libagora-ffmpeg.so` | x64 `libagora_rtc.so` 的 `NEEDED` 相依 |
| `libagora-soundtouch.so` | 同上 |

Agora 發佈的 aarch64 RTSA tarball 只含三個庫
（`libagora_rtc_sdk.so`、`libaosl.so`、`libagora-fdkaac.so`），
而 x64 的 TEN 套件含八個。若 aarch64 的 wrapper 以相同 link line 編譯，
載入時會找不到這兩個庫。

**替代方案**：確認 aarch64 wrapper 可以不連結它們，則此項可免。

## 三、技術評估：改動範圍很小

依先前的符號分析（`package_agora_rtc_sdk_arm64.sh` 內記錄）：

- x64 的 `libagora_rtc.so` 從 SDK 只解析 **兩個符號**：
  `createAgoraService`、`getAgoraSdkVersion`
- 這兩個符號 **aarch64 的 RTSA SDK 都有**
- wrapper 不直接引用任何 `agora::` 類別符號，全部經由 vtable，
  也從未使用 `createAgoraRtcEngine` 或 `IRtcEngine`

結論：wrapper 使用的是 **low-level / server API**，正是 aarch64 RTSA SDK 提供的部分。
aarch64 tarball 缺少高階 client API 的 45 個 header
（`IAgoraRtcEngine.h`、`IAgoraRtcEngineEx.h`、`IAgoraMediaEngine.h` 等）**不影響**。

**所需的只是把既有的 wrapper 源碼以 aarch64 目標編譯一次。**

## 四、我方已完成的部分

`ai_agents/agents/scripts/package_agora_rtc_sdk_arm64.sh` 已可將 Agora 的
aarch64 RTSA tarball 轉換成 TEN 的 `agora_rtc_sdk` system 套件：

- 驗證 tarball 內的庫確實為 aarch64
- 將扁平的 `include/` 對應到 x64 套件使用的
  `include/rtc/low_level_api/include/` 佈局
- 標記 `supports: linux/arm64`
- 產生 `PROVENANCE.md` 記錄來源與差異

已端到端驗證：符合 manifest JSON schema、可解析 `=4.4.32-141` 的精確 pin、
可安裝進 tenapp、lock 正確記錄 `{"os": "linux", "arch": "arm64"}`。

**因此 SDK 側已備妥，wrapper 一旦提供即可接上。**

## 五、待確認事項

1. **另外三個庫是否為必要相依**

   x64 套件另含 `libagora_stt_ag_extension.so`、`libagora_stt_ms_extension.so`、
   `libagora_mcc_ysd_extension.so`。是否被 wrapper 使用尚未確認，
   需對 x64 的 `libagora_rtc.so` 執行 `readelf -d | grep NEEDED` 才能定案。

2. **版本號差異**

   `agora_rtc` extension 精確釘住 `"version": "=4.4.32-141"`，
   而 Agora 的 aarch64 tarball 回報 `4.4.32`，無 build suffix。
   同一 minor 版本，不同 build。需確認 aarch64 側應標示哪個版本號。

3. **registry 上架**

   即使 SDK 側可由我方自行打包，正式使用仍需 `agora_rtc_sdk` 的 aarch64
   版本上架到 TEN registry，否則 `tman install` 無法解析。

## 六、影響

| | 現況 | 提供後 |
| --- | --- | --- |
| 可運行的 example | 1 / 26 | 25 / 26 |
| 傳輸層 | 僅 WebSocket（單一連線、無 NAT 穿透、無抖動緩衝、無回音消除） | RTC 完整能力 |
| 部署場景 | 區域網路、單人 | 跨網際網路、行動網路、多人 |
