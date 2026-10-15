# voice-assistant-ambarella

A voice agent that runs entirely on an Ambarella N1-655 board: ASR, LLM and TTS
are all on-board daemons, with Agora RTC as the transport.

```
G.722 16k ──► agora_rtc ──► streamid_adapter ──┬──► stt   (asr_d, 16k in)
                                               └──► vad   (turn boundaries)
                                                      │
                                     main_control ◄───┘
                                          │
                                          ▼
                    llm (Qwen3.5-4B on vLLM)    ──► tts (tts_d) ──► agora_rtc
```

## Before starting

1. Load the Cavalry firmware and make the daemons executable:

   ```bash
   cavalry_load -f /lib/firmware/cavalry.bin -r
   chmod +x /home/lychee/asr_tts_demo/app_demo/{asr_d,tts_d}
   ```

2. Start the board's LLM, Qwen3.5-4B on vLLM (installed by
   `tools/ambarella/install_qwen35_vllm.sh`; once
   `tools/ambarella/install_board_services.sh` has run, it starts at power-on):

   ```bash
   tools/ambarella/serve_qwen35.sh start
   ```

   Loading takes about a minute. It stops the vendor's DeepSeek 7B daemon if
   that runs: the two cannot share the NPU.

3. **Move TEN's API server off port 8080.** The board's LLM demo server binds
   port 8080 and `run_llm_demo.sh` has no port flag, while TEN's Go API server
   also defaults to port 8080 (`ai_agents/server/main.go:92`, driven by the
   `SERVER_PORT` environment variable, which defaults to `8080` in
   `.env.example`). Running both natively on the board collides. The
   resolution is on TEN's side — set `SERVER_PORT` to something else before
   `task run`:

   ```bash
   export SERVER_PORT=8090
   ```

4. Do not run the LLM demo and the LLaVA demo together — they share a library
   that does not support it.

## Running

```bash
task install
task run
```

## Sample rates

16 kHz throughout, in both directions. RTC decodes G.722 to 16 kHz PCM, which
is exactly what `asr_d` wants. On the way back, `tts_d` emits 22050 Hz and
`ambarella_tts_python` resamples to 16000 before handing frames to RTC.

## Known constraints

- **Language and voice are fixed per session.** `asr_d`'s `--language` and
  `tts_d`'s `--speaker_id` are startup flags.
- **No partial ASR results.** `asr_d` is batch-only, so text appears once per
  turn, after the VAD closes it.
- **`--max_user 1`** on the LLM demo means one turn at a time.
- **Adding or removing a graph is not hot-reloadable.** Restart the server and
  the playground; the frontend caches `/graphs`.

## Design

`docs/superpowers/specs/2026-09-10-ambarella-asr-tts-extensions-design.md`
