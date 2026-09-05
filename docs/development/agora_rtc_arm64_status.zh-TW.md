---
title: agora_rtc aarch64 支援現況
---

# agora_rtc aarch64 支援現況

**結論:已可用。** RTC 在 aarch64 上原生運行,實測連上 Agora、發佈音軌、建立 data stream。

先前的版本認為需要 Agora 提供四個 `.so`。那個結論建立在「wrapper 無源碼」的前提上,
拿到源碼後不成立 —— **wrapper 的邏輯完全不用改**。

## 需要什麼

| 項目 | 來源 |
|---|---|
| `agora_rtc` wrapper 源碼 | 向 Agora 索取 |
| `agora_rtc_sdk` 的 aarch64 tarball | 版本必須對上 wrapper manifest 的精確 pin |

兩者的配對關係:

| wrapper | 需要的 SDK |
|---|---|
| `0.23.9-t1`（examples 目前 pin 的版本） | `=4.4.32-141` |
| `0.26.0-rc14`（撰寫時的 master） | `=4.4.32-175` |

**配錯會在編譯期失敗,而且改版本號沒有用。** `0.26` 呼叫 `setTotalExtraSendMs`、
override `onCustomUserInfoUpdated`,這兩個 API 在 141 的 header 裡不存在。

## 三處改動,都與 RTC 邏輯無關

| # | 檔案 | 改動 | 為什麼 |
|---|---|---|---|
| 1 | `manifest.json` | `supports` 加 `{"os":"linux","arch":"arm64"}` | 不宣告的話 tman 拒絕在 arm64 安裝 |
| 2 | `BUILD.gn` | `resources` 移除 `lib/liblinux_audio_hy_extension.so` | 該檔只有 x86-64。runtime 會 dlopen addon `lib/` 下每個 `.so`,架構不符是載入失敗而非略過 |
| 3 | `src/` 27 個檔 | 補 `#include <cstdint>` | GCC 13 起不再遞移引入。**x64 用 GCC 14 一樣會失敗**,與 arm64 無關 |

第 3 項是 vendor 源碼的缺陷,建議回報。

## 建置指令

與 Agora 自己的 Taskfile 只差一個字:

```
官方   tgn gen linux x64   release -- is_clang=false
arm64  tgn gen linux arm64 release -- is_clang=false
```

## 腳本

`ai_agents/agents/scripts/` 底下四支,每支都驗證自己的產出:

| 腳本 | 用途 |
|---|---|
| `package_agora_rtc_sdk_arm64.sh` | Agora tarball → TEN system 套件 |
| `build_agora_rtc_arm64.sh` | 編譯 wrapper |
| `install_agora_rtc_arm64.sh` | 裝進 example 的 tenapp |
| `finish_example_install_arm64.sh` | 補完 `task install` 其餘步驟 |

詳見 `arm64_build.md` 的 **Building agora_rtc for aarch64**。

## 驗證通過的證據

```
[agora_rtc] on_start() done
onConnecting:  channelId <channel>, state 2
onConnected:   localUserId <uid>, state 3, reason 1
               sid(<session id>)
custom audio_track created
audio track published
onAudioTrackPublishSuccess
```

`sid` 由 Agora 伺服器回傳,可據此區分「真的連上」與「本地初始化完成但未觸網」。

## 仍待 Agora 處理的

| 項目 | 影響 |
|---|---|
| registry 未發佈 arm64 的 `agora_rtc` / `agora_rtc_sdk` | 每個使用者都要自行建置與手動放置 |
| `liblinux_audio_hy_extension.so` 只有 x86-64 | 目前從 arm64 套件排除。若該功能仍需要,需 aarch64 build |
| 源碼缺 `#include <cstdint>` | 在 GCC 13+ 上編不過,x64 亦然 |
