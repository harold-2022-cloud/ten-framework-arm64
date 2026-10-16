# Meeting demo screen

The board's meetings on the board's own display: a service that reads them
from disk (`MEETINGS_DIR`) and a page a full-screen browser shows on the
HDMI screen. A phone records and uploads as before; the screen follows on
its own -- the stages as they happen, who spoke when once the speakers are
told apart, the summary at the end. Everything runs on the board.

A Traditional Chinese edition is at [`README.zh-TW.md`](README.zh-TW.md).
The design is `docs/superpowers/specs/2026-10-14-meeting-demo-screen-design.md`
(local).

## On the board

```bash
tools/ambarella/install_board_services.sh            # the meeting-screen service, with the rest
tools/ambarella/install_board_services.sh --kiosk on # the page full screen when the desktop starts
tools/ambarella/set_desktop_autologin.sh             # optional: the desktop -- and so the screen -- at every boot
tools/ambarella/prepare_meeting_demo.sh              # before a demo: is everything ready?
```

The showcase -- the finished meeting PageDown shows -- is set in
`ai_agents/.env` as `MEETING_SCREEN_SHOWCASE=<meeting_id>`, then
`install_board_services.sh` again. Without autologin, someone logs in on
the board after a power cut and the screen comes back by itself.

| Key | |
| --- | --- |
| PageDown, → | the showcase |
| PageUp, ←, Esc | back to the live meeting |
| ↑ ↓ | scroll the transcript |
| L | every meeting; ↑ ↓ and Enter to pin one |
| Alt+F4 | leave the full-screen browser |

## Demo day

1. The day before: `tools/ambarella/prepare_meeting_demo.sh`, all ok. Within
   half an hour of going on: `tools/ambarella/prepare_meeting_demo.sh --warm`,
   so the first upload need not wait for the meeting worker (an idle one
   ends after 30 minutes).
2. Opening: the ready screen. "All of the meeting AI is on this box."
3. A short live meeting: two or three people, about a minute, from prepared
   questions, the phone on the table. Enter the real head count in the app.
   Each turn about ten seconds or more, without talking over each other:
   a voice heard too briefly cannot be told apart.
4. Upload: the screen switches by itself; talk through each stage as it
   lights.
5. The reveal: the transcript takes the speakers' colours, the timeline,
   the summaries.
6. Scale: PageDown for the full showcase meeting -- its length, its
   speakers, how long the board took; PageUp to come back.
7. If the live meeting fails, the screen says why; go straight to step 6.

## Without a board

```bash
cd ai_agents/meeting-screen
python3 tools/simulate_meeting.py ../../path/to/record.json --meetings-dir /tmp/sim &
python3 -m meeting_screen --meetings-dir /tmp/sim
```

Then open `http://localhost:8766/` (from Windows too, under WSL2): a finished
meeting replayed through every stage in about two minutes. If something else
holds 8766, add `--listen 127.0.0.1:18766` and open that port instead.

## Tests

```bash
docker exec ten_agent_dev bash -c "cd /app/meeting-screen && python3 -m pytest tests -q -p no:cacheprovider"
node --test 'ai_agents/meeting-screen/tests/*.test.mjs'
```

The service uses only the standard library: the board runs it under its own
`python3`, which has none of the meeting extensions' packages.
