#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Run text through sherpa_onnx_tts_python and write a WAV to listen to.

The pipeline logs say what text reached the extension and how much audio left
it, and they cannot say whether the audio is right. This runs the extension's
own code -- sanitise, split_for_latency, generate, float_to_pcm16,
resample_pcm16, the 20 ms framing -- over text you supply, and writes exactly
the bytes that would have gone to RTC.

  python3 tools/ambarella/speak_text.py "老板问两个员工：“你们两个月的工资是多少？"
  python3 tools/ambarella/speak_text.py --from-log /tmp/task_run.log
  python3 tools/ambarella/speak_text.py --no-split "不切，聽原本的樣子"

--from-log replays every piece the pipeline sent to TTS in that log, in order,
as one file: what the assistant said in that session, reproduced offline.

Writes nothing but the WAV. Does not touch the running app.
"""

import argparse
import asyncio
import os
import re
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_ROOT = os.path.join(
    HERE, "..", "..", "ai_agents", "agents", "ten_packages", "extension"
)
sys.path.insert(0, os.path.abspath(EXT_ROOT))


def texts_from_log(path):
    """Every piece the pipeline handed to TTS, in order.

    A text containing newlines breaks the log line, so the continuation is
    taken until the next timestamped line -- otherwise the first sentence of
    every reply is lost, which is how it was misread once already.
    """
    with open(path, encoding="utf-8", errors="replace") as handle:
        raw = handle.read()
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    lines = raw.splitlines()
    start = re.compile(r"^(\[[a-z0-9_]+\] 2026-|\d{4}/|Request to|\[GIN\])")
    found = []
    i = 0
    while i < len(lines):
        m = re.search(r"Requesting TTS for text: (.*)$", lines[i])
        if not m:
            i += 1
            continue
        text = m.group(1)
        j = i + 1
        while j < len(lines) and not start.match(lines[j]):
            text += "\n" + re.sub(r"^\[[a-z0-9_]+\] ", "", lines[j])
            j += 1
        text = text.split(", text_input_end")[0]
        if text.strip():
            found.append(text)
        i = j
    return found


async def synthesise(client, texts, request_id):
    """Collect what the extension would have sent to RTC."""
    from ten_ai_base.tts2_http import TTS2HttpResponseEventType

    audio = bytearray()
    frames = 0
    for index, text in enumerate(texts):
        async for chunk, kind in client.get(text, f"{request_id}-{index}"):
            if kind == TTS2HttpResponseEventType.RESPONSE and chunk:
                audio.extend(chunk)
                frames += 1
            elif kind == TTS2HttpResponseEventType.ERROR:
                print(f"  ERROR: {chunk!r}")
    return bytes(audio), frames


class _Env:
    """The extension logs through ten_env; here that goes to the terminal."""

    def _say(self, message, **_kwargs):
        print(f"  {message}")

    log_debug = log_info = log_warn = log_error = _say


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="*", help="what to say")
    parser.add_argument("--from-log", help="replay every piece a log sent")
    parser.add_argument(
        "--voice-dir",
        default=os.path.expanduser(
            "~/piper_tts/vits/vits-piper-zh_CN-huayan-medium"
        ),
    )
    parser.add_argument("--out", default="/tmp/speak_text.wav")
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument(
        "--no-split",
        action="store_true",
        help="hand each text over whole, to hear what splitting changed",
    )
    args = parser.parse_args()

    if args.from_log:
        texts = texts_from_log(args.from_log)
        if not texts:
            sys.exit(f"no 'Requesting TTS for text:' lines in {args.from_log}")
    elif args.text:
        texts = [" ".join(args.text)]
    else:
        sys.exit("give some text, or --from-log")

    if not os.path.isdir(args.voice_dir):
        sys.exit(f"no voice at {args.voice_dir}")

    from sherpa_onnx_tts_python.config import SherpaOnnxTTSConfig
    from sherpa_onnx_tts_python.sherpa_onnx_tts import SherpaOnnxTTSClient

    config = SherpaOnnxTTSConfig(
        voice_dir=args.voice_dir,
        output_sample_rate=args.rate,
        min_chars_to_split=0 if args.no_split else 8,
    )
    client = SherpaOnnxTTSClient(config=config, ten_env=_Env())

    print(f"voice   {args.voice_dir}")
    print(f"split   {'off' if args.no_split else 'on'}")
    print(f"texts   {len(texts)}")
    for text in texts:
        print(f"  {text!r}")
    print()

    audio, frames = asyncio.run(synthesise(client, texts, "offline"))

    with wave.open(args.out, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(args.rate)
        out.writeframes(audio)

    seconds = len(audio) / 2 / args.rate
    print()
    print(f"wrote   {args.out}")
    print(f"        {seconds:.2f}s, {frames} frames of 20 ms at {args.rate} Hz")
    print()
    print("  These are the bytes the extension would have handed to RTC.")
    print("  If this sounds right, the fault is not in the TTS extension.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
