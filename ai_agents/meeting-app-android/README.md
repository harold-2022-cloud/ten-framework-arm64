# Meeting minutes — Android prototype

Records a meeting on the phone, sends it to the meeting-room board, and shows
the minutes the board writes: conclusion, topics with summaries, and every line
with its time and speaker. Speakers can be renamed, and the record shared as
text.

It talks to the board's `meeting_minutes` graph over the LAN, the same API the
curl walk-through in `docs/development/board_quickstart.md` uses:

| Port | What | The app uses it for |
| --- | --- | --- |
| 8081 | Go API server | `/start` of the meeting worker, always channel `meeting-room`; `/ping` when it is already running |
| 8765 | uploader, inside that worker | upload, state and progress, `record.json` |

## Build

JDK 17 and the Android SDK (platform 35, build-tools 35):

```bash
echo "sdk.dir=/path/to/android-sdk" > local.properties
./gradlew :app:testDebugUnitTest   # logic and the board API, against mock servers
./gradlew :app:assembleDebug       # app/build/outputs/apk/debug/app-debug.apk
```

The APK is debug-signed. Install it with `adb install -r app-debug.apk`, or copy
it to the phone and open it (allow installs from that app when asked).
Android 10 or later.

## Use

1. **Settings**: enter the board's IP. The phone must be on the board's network.
   Fill in the token only if the board sets `MEETING_AUTH_TOKEN`. Pick
   Traditional or Simplified as the default for the record.
2. **Start recording**: the whole meeting goes into one Ogg-Opus file
   (16 kHz mono), the only format the board takes. It keeps recording with the
   screen off or the app in the background.
3. **Stop**, then fill in the form. A title is optional. The number of
   attendees is required; err high, because too low merges two people into
   one. Choose the script for this meeting, then upload.
4. The details screen shows progress while it is open, checking the board
   every 30 s. Nothing polls in the background. A notification fires at upload
   time + the recording's length + 2 minutes, and processing measured 0.93× the
   recording's length on the board. Tapping the notification opens the meeting.

Every meeting goes to the board's one meeting worker, and the app never
stops it: it may be in the middle of someone else's meeting. The board reaps
it about ten minutes after its last meeting ends. If you open a meeting
later than that, the app starts the worker again to read the record. Once
fetched, the record lives on the phone and reads offline.

For how the code is laid out and the rules it keeps, see `DEVELOPMENT.md`
(Traditional Chinese).

## Prototype limits

- One meeting at a time on the board: an upload while another meeting is
  still being processed is refused (409), and the app says so.
- A recording the app was killed away from is offered again on the next
  launch. Its length is read off the file, so the length shown can be a few
  seconds out.
- HTTP in the clear on the LAN (`network_security_config.xml`); the token is
  the only protection.
- No iOS yet.
