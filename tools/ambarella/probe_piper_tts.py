#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Measure a sherpa-onnx VITS voice on the board before wrapping it.

Two things are unknown until this runs on the hardware: how fast the CPU
synthesises, and what the voice sounds like. The first decides whether the
extension is worth building; the second cannot be measured at all, so this
writes a WAV to listen to.

Usage, after setup_cpu_asr_tts_arm64.sh has fetched a voice:

  python3 probe_piper_tts.py
  python3 probe_piper_tts.py --voice-dir ~/piper_tts/vits/vits-melo-tts-zh_en

Exit code is 0 only when every sentence synthesised.
"""

import argparse
import glob
import os
import sys
import time
import wave

DEFAULT_ROOT = os.path.expanduser("~/piper_tts/vits")

# Mandarin, because that is what the board is being brought up in. Short,
# medium and long, since synthesis cost is not linear in every engine.
SENTENCES = [
    "你好。",
    "今天天氣如何?",
    "我是一個在安霸開發板上執行的語音助理,可以幫你回答問題。",
    "這是一段比較長的測試句子,用來看看合成時間會不會隨著文字長度線性增加,"
    "因為如果不是線性的,那麼把長句切短就有意義。",
    # Several sentences, the shape an LLM reply actually has. generate()
    # hands back one chunk per sentence, so 'first' should be far below
    # 'synth' here while the single-sentence rows above have them equal.
    "好的,我幫你查一下。台北今天多雲,氣溫攝氏二十六度。"
    "下午可能會下雨,出門記得帶傘。還需要我查別的城市嗎?",
]


def find_voice_dir(explicit):
    if explicit:
        return os.path.expanduser(explicit)
    found = sorted(glob.glob(os.path.join(DEFAULT_ROOT, "*")))
    found = [d for d in found if os.path.isdir(d)]
    if not found:
        sys.exit(
            f"no voice under {DEFAULT_ROOT}.\n"
            "Run: ai_agents/agents/scripts/setup_cpu_asr_tts_arm64.sh --asr-from-source"
        )
    if len(found) > 1:
        print(f"  several voices present, using {os.path.basename(found[0])}")
        print("  pass --voice-dir to choose another:")
        for d in found:
            print(f"    {d}")
    return found[0]


def find_one(voice_dir, pattern, required=True):
    hits = sorted(glob.glob(os.path.join(voice_dir, pattern)))
    if not hits:
        if required:
            sys.exit(f"no {pattern} in {voice_dir}")
        return ""
    return hits[0]


def build_tts(voice_dir, num_threads):
    import sherpa_onnx as so

    # The model file name varies between bundles, so find it rather than
    # assume. tokens.txt and espeak-ng-data are why the voice has to come
    # from sherpa-onnx's repackaged bundle and not from Hugging Face.
    model = find_one(voice_dir, "*.onnx")
    tokens = find_one(voice_dir, "tokens.txt")
    data_dir = os.path.join(voice_dir, "espeak-ng-data")
    dict_dir = os.path.join(voice_dir, "dict")
    lexicon = find_one(voice_dir, "lexicon.txt", required=False)

    vits = so.OfflineTtsVitsModelConfig(
        model=model,
        tokens=tokens,
        data_dir=data_dir if os.path.isdir(data_dir) else "",
        dict_dir=dict_dir if os.path.isdir(dict_dir) else "",
        lexicon=lexicon,
    )
    config = so.OfflineTtsConfig(
        model=so.OfflineTtsModelConfig(vits=vits, num_threads=num_threads)
    )
    print(f"  model     {os.path.basename(model)}")
    print(f"  tokens    {os.path.basename(tokens)}")
    print(f"  data_dir  {'yes' if vits.data_dir else 'absent'}")
    print(f"  dict_dir  {'yes' if vits.dict_dir else 'absent'}")
    print(f"  lexicon   {'yes' if lexicon else 'absent'}")

    started = time.monotonic()
    tts = so.OfflineTts(config)
    print(f"  loaded in {time.monotonic() - started:.1f}s")
    return tts


def write_wav(path, samples, sample_rate):
    import numpy as np

    pcm = np.clip(np.asarray(samples) * 32767.0, -32767, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-dir", default="")
    parser.add_argument("--num-threads", type=int, default=1)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--out-dir", default="/tmp")
    args = parser.parse_args()

    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        sys.exit("sherpa-onnx is not installed. Run: pip install --user sherpa-onnx")

    voice_dir = find_voice_dir(args.voice_dir)
    print(f"\n=== Voice: {os.path.basename(voice_dir)}")
    tts = build_tts(voice_dir, args.num_threads)
    print(f"  sample rate {tts.sample_rate} Hz, {tts.num_speakers} speaker(s)")

    print(f"\n=== Synthesis, {args.num_threads} thread(s)")
    # Time to the first chunk is what a listener feels; the total is what the
    # CPU costs. generate() calls back once per sentence as it goes, so the
    # two differ by most of a multi-sentence reply -- and the cloud provider
    # this replaces answered its first byte in 361 ms.
    print(
        f"  {'chars':>5}  {'first':>7}  {'synth':>7}  {'audio':>7}  "
        f"{'RTF':>5}  {'calls':>5}  text"
    )
    failures = 0
    for index, text in enumerate(SENTENCES):
        started = time.monotonic()
        first_at = [None]
        calls = [0]

        def on_chunk(samples, progress, _f=first_at, _c=calls):
            # Return non-zero to CONTINUE, zero to stop -- the opposite of
            # what generate()'s own docstring says, measured against
            # sherpa-onnx 1.13.8. Returning 0 here truncates every reply to
            # its first sentence, which is how the mistake shows up.
            _c[0] += 1
            if _f[0] is None:
                _f[0] = time.monotonic()
            return 1

        audio = tts.generate(
            text, sid=0, speed=args.speed, callback=on_chunk
        )
        elapsed = time.monotonic() - started
        first = (first_at[0] - started) if first_at[0] else elapsed
        seconds = len(audio.samples) / audio.sample_rate if audio.sample_rate else 0
        if seconds <= 0:
            print(f"  {len(text):>5}  FAILED: no audio for {text!r}")
            failures += 1
            continue
        # Real-time factor: below 1.0 means it synthesises faster than it
        # plays, which is what a conversation needs.
        print(
            f"  {len(text):>5}  {first:>6.2f}s  {elapsed:>6.2f}s  "
            f"{seconds:>6.2f}s  {elapsed / seconds:>5.2f}  {calls[0]:>5}  "
            f"{text[:24]}"
        )
        out = os.path.join(args.out_dir, f"piper_probe_{index}.wav")
        write_wav(out, audio.samples, audio.sample_rate)

    print(f"\n  WAVs in {args.out_dir}/piper_probe_*.wav -- listen before trusting")
    if failures:
        print(f"\n  {failures} sentence(s) produced no audio")
        return 1
    print("\n  Every sentence synthesised.")
    print("  RTF below 1.0 means it keeps up with speech; the lower the more")
    print("  headroom for the LLM and the runtime sharing these cores.")
    print()
    print("  'first' is the number a listener feels. Compare it against the")
    print("  cloud provider being replaced: elevenlabs answered its first")
    print("  byte in 361 ms and 454 ms, measured on this board on 2026-09-14.")
    print("  'calls' is how many times generate() handed audio back, one per")
    print("  sentence. A single call for multi-sentence text means the")
    print("  callback's return value stopped generation early.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
