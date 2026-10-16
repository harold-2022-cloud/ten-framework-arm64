# 會議紀錄演示大屏

讓板子自己的螢幕顯示板子上的會議。由兩部分組成：一個從磁碟（`MEETINGS_DIR`）讀會議的服務，以及一個由全螢幕瀏覽器在 HDMI 螢幕上顯示的頁面。手機照舊錄音、上傳，大屏自己跟著切換：處理到哪個階段、分辨出說話人後誰在什麼時候說話、最後的摘要。全部在板子上完成。

英文版是 [`README.md`](README.md)。設計文件是 `docs/superpowers/specs/2026-10-14-meeting-demo-screen-design.md`（本地）。

## 在板子上

```bash
tools/ambarella/install_board_services.sh            # 跟其他服務一起裝好 meeting-screen 服務
tools/ambarella/install_board_services.sh --kiosk on # 桌面一啟動就全螢幕開頁面
tools/ambarella/set_desktop_autologin.sh             # 選用：每次開機自動登入桌面，大屏也跟著出來
tools/ambarella/prepare_meeting_demo.sh              # 演示前：檢查是否都就緒
```

範例會議（按 PageDown 顯示的那場已完成會議）設定在 `ai_agents/.env`：`MEETING_SCREEN_SHOWCASE=<meeting_id>`，設好後再跑一次 `install_board_services.sh`。沒開自動登入的話，斷電重開後要有人在板子上登入，登入後大屏會自己出來。

| 按鍵 | |
| --- | --- |
| PageDown、→ | 範例會議 |
| PageUp、←、Esc | 回到現場那場 |
| Home | 回到待命畫面，直到下一次上傳（例如彩排之後） |
| ↑ ↓ | 捲動逐字稿 |
| L，或右上角「會議紀錄」 | 板子上所有會議的清單；用 ↑ ↓ 和 Enter，或直接點一場，進去看 |
| 右上角「回到現場」 | 回到即時畫面，同 PageUp |
| Alt+F4 | 離開全螢幕瀏覽器 |

## 演示當天

1. 前一天：`tools/ambarella/prepare_meeting_demo.sh`，全部 ok。上台前半小時內再跑 `tools/ambarella/prepare_meeting_demo.sh --warm`，讓第一次上傳不用等會議處理程式啟動（閒置 30 分鐘就會被收掉）。
2. 開場：螢幕是待命畫面；彩排的結果還在畫面上的話，按 Home。「會議 AI 全部在這台裝置上。」
3. 現場小會：兩三個人照預先寫好的題目討論約一分鐘，手機放桌上錄音。App 裡填與會人數，有幾人發言由板子自己判斷。每次發言約十秒以上，不搶話：聽得太短的聲音分辨不出是誰。
4. 上傳：大屏自己切換，主講人照亮起的階段講解：收到錄音 → 分辨說話人（預估進度條，分辨完之前沒有東西可看）→ 轉文字（逐字稿一句一句出現，還是灰色）→ 對齊說話人 → 話題摘要 → 會議結論。
5. 揭曉：逐字稿依說話人上色、時間軸、摘要。
6. 規模：按 PageDown 看完整的範例會議，講它的長度、說話人數、板子處理了多久；按 PageUp 回來。
7. 現場那場失敗時，畫面會顯示原因，直接進第 6 步。

## 不用板子預覽

```bash
cd ai_agents/meeting-screen
python3 tools/simulate_meeting.py ../../path/to/record.json --meetings-dir /tmp/sim &
python3 -m meeting_screen --meetings-dir /tmp/sim
```

然後打開 `http://localhost:8766/`（WSL2 下從 Windows 也能開），會看到一場已完成的會議約兩分鐘走完每個階段。8766 被別的程式佔用時，加上 `--listen 127.0.0.1:18766`，改開那個埠。

## 測試

```bash
docker exec ten_agent_dev bash -c "cd /app/meeting-screen && python3 -m pytest tests -q -p no:cacheprovider"
node --test 'ai_agents/meeting-screen/tests/*.test.mjs'
```

服務只用標準庫：板子用自己的 `python3` 跑它，那個 python 沒有會議擴充所用的套件。
