# Meeting minutes — Android prototype

Records a whole meeting on the phone and sends it to the meeting-room board;
once the board has processed it, the phone shows the minutes: the meeting's
conclusion, each topic's key points, and every line with its time and
speaker. Speakers can be given their real names, and the record shared.
繁體中文：[README.zh-TW.md](README.zh-TW.md).

All processing happens on the board, with no internet. Any phone with the app
may send a meeting at any time; the board processes one at a time and queues
the rest, first come first.

## 1. The board, once

Set the board up as
[`docs/development/board_quickstart.md`](../../docs/development/board_quickstart.md)
says (`install_board_arm64.sh`, the meeting models, `ai_agents/.env`). Then,
once, from the repo root, in the terminal you run `task run` from (sudo asks
for your password):

```bash
tools/ambarella/install_board_services.sh
```

It makes the vendor's LLM and the API server services that start at power-on,
so the board takes a phone's request with nothing typed afterwards.

- Check: `tools/ambarella/install_board_services.sh --status`. Both services
  `active`, one `test_llm` and one `test_llm_client`, the API server answering.
- **Run `install_board_services.sh` again after every `git pull`**: it rebuilds
  and restarts what changed (not while a meeting is being processed; it then
  asks to be run again later).

It talks to the board's `meeting_minutes` graph over the LAN, the same API the
curl walk-through in the quickstart uses:

| Port | What | The app uses it for |
| --- | --- | --- |
| 8081 | Go API server | `/start` of the meeting worker, always channel `meeting-room`; `/ping` when it is already running |
| 8765 | uploader, inside that worker | upload, state, place in the queue, `record.json` |

## 2. The phone, once

1. Install the APK. Android 10 or later. The first install asks to allow
   installing unknown apps; installing a newer build over it keeps the phone's
   meetings and settings. The current version is **0.2.0**, shown at the
   bottom of the app's settings.
2. Put the phone on the board's network. Open `http://<board IP>:8081/graphs`
   in the phone's browser: JSON means the board is reachable.
3. In the app's settings (設定): the board's IP only, e.g. `192.168.1.50` (no
   `http://`, no port); the token only if the board sets `MEETING_AUTH_TOKEN`;
   Traditional or Simplified as the default script. Save (儲存).

## 3. A meeting

1. **開始錄音** (start recording). Allow the microphone and notifications the
   first time. It keeps recording with the screen off or the app in the
   background, into one 16 kHz mono Ogg-Opus file the app writes itself.
2. **散會，結束錄音** (end), and confirm.
3. The form: a title (optional); the number of attendees (required; err high,
   because too low merges two people into one); the script for this meeting.
   **上傳到會議室的板子** uploads; 捨棄這段錄音 throws the recording away.
4. The meeting's page shows progress (section 4). Processing takes about the
   meeting's length. No need to wait on the screen: **a notification comes at
   the estimated finish** (upload time + any queue wait + the recording's length
   + 2 minutes), and tapping it opens the meeting.
5. Done, the page shows the **conclusion**, then each **topic** -- the board
   splits the meeting at pauses -- with its key points and every line as
   `[time] speaker N: text`.
6. **說話人命名** renames speakers; the names live on this phone only.
7. **分享** shares the whole record, with the names, as text.
8. **刪除** (top right of a meeting's page) removes its recording and record
   from the phone; the board keeps its copy.

## 4. What the states mean

| Shown | Meaning |
| --- | --- |
| 還沒上傳 | the form is done, not uploaded yet |
| 連線到板子… | waking the board's meeting service |
| 上傳中… | uploading |
| 上傳失敗 | the upload failed, with the reason; upload again |
| 排隊中，前面還有 N 場 | the board is processing someone else's meeting; this one waits and starts by itself |
| 排隊中，下一個就輪到 | next in line |
| 準備中 / 轉成文字 n / N / 整理說話人 / 寫摘要 n / N / 寫結論 | being processed: reading, transcribing, matching voices across the meeting, key points, conclusion |
| 完成 | done; the record is on the phone and reads offline |
| 錄音裡沒有偵測到說話 | no speech in the recording |
| 處理失敗：… | processing failed, with the reason; upload again to start over |

## 5. When something goes wrong

| Seen | Do |
| --- | --- |
| cannot reach the board (連不上板子) | same network, the right IP, the board on; `install_board_services.sh --status` on the board |
| the board asks for a token | enter the board's `MEETING_AUTH_TOKEN` in settings |
| 無法開始錄音 on the home screen | the microphone is taken (a phone call, say); end it and try again |
| upload failed / processing failed | upload again; the recording stays on the phone |
| the app was killed after a recording ended | open it again: the form comes back |
| the app was killed mid-upload | open the meeting and upload again |
| it takes long | about the meeting's length, plus the meetings queued before it |
| wrong recording format (錄音格式不對) | should not happen since 0.2.0, which writes its own file; report it with the version |

On the board: `check_meeting_worker.py` (queue, reaping, restarting; no phone),
`verify_meeting_board.sh --no-meeting --phone` (follows a meeting from a
phone), `rerun_meeting_worker_check.sh` (clears a stuck run first). Logs:
`/tmp/task_run.log`, and `/tmp/log.txt` for the vendor's LLM.

## Build

JDK 17 and the Android SDK (platform 35, build-tools 35):

```bash
echo "sdk.dir=/path/to/android-sdk" > local.properties
./gradlew :app:testDebugUnitTest   # logic, the recording file, the board API against mock servers
./gradlew :app:assembleDebug       # app/build/outputs/apk/debug/app-debug.apk
```

The APK is debug-signed. Install it with `adb install -r app-debug.apk`, or copy
it to the phone and open it. How the code is laid out and the rules it keeps:
[DEVELOPMENT.md](DEVELOPMENT.md) (繁體中文：[DEVELOPMENT.zh-TW.md](DEVELOPMENT.zh-TW.md)).

## Prototype limits

- The board processes one meeting at a time; the others queue.
- Android only (10 or later); no iOS yet.
- Plain HTTP on the LAN (`network_security_config.xml`); the token is the only
  protection.
- A finished meeting's recording is not deleted by itself; 刪除 on its page does.
- Traditional changes character forms, not vocabulary: 網絡 and 反饋 stay.
