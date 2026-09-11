#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A stand-in for asr_d / tts_d that speaks the same line protocol.

Driven by --scenario. Every other flag is ignored, so the real argv the
extension builds can be passed through unchanged; tests select a scenario by
adding it to the extension's `params`, which the pass-through expands into
--scenario <name>.
"""

import argparse
import struct
import sys
import time
import wave


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit_noise() -> None:
    """Imitate EazyAI's own Notice-level chatter on the same stdout."""
    emit("[INFO] [01-01 17:33:55] Device ENABLE: fd_dev: 5, net_type: 9")
    emit("[NOTICE] cavalry: vp memory 41f00000 reserved")


def write_wav(path: str, rate: int, seconds: float) -> int:
    frames = int(rate * seconds)
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(struct.pack("<%dh" % frames, *([0] * frames)))
    return frames


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="ok")
    parser.add_argument("--ready-token", default="READY asr")
    parser.add_argument("--stub-text", default="hello world")
    parser.add_argument("--stub-rate", type=int, default=22050)
    parser.add_argument("--stub-delay", type=float, default=0.0)
    # Underscore, not hyphen: set via params.infer_delay, which the
    # extension's pass-through expands verbatim into --infer_delay.
    parser.add_argument("--infer_delay", type=float, default=0.0)
    args, _ignored = parser.parse_known_args()

    scenario = args.scenario

    if scenario == "no_ready":
        time.sleep(3600)
        return 0

    if scenario == "die_on_load":
        emit("ERR init")
        return 1

    if scenario == "noise":
        emit_noise()

    if args.stub_delay:
        time.sleep(args.stub_delay)

    emit(args.ready_token)

    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()

        if line == "QUIT":
            emit("OK bye")
            return 0

        if not line.startswith("INFER"):
            emit("ERR unknown_cmd")
            continue

        if scenario == "noise":
            emit_noise()

        if scenario == "die_on_infer":
            return 1

        if scenario == "hang_on_infer":
            time.sleep(3600)
            continue

        if scenario == "no_speech":
            emit("ERR no speech.")
            continue

        if scenario == "err_infer":
            emit("ERR infer")
            continue

        if args.ready_token == "READY tts":
            out_path = line.rsplit(" ", 1)[1]
            frames = write_wav(out_path, args.stub_rate, 0.2)
            emit(f"OK wav={out_path} frames={frames}")
            continue

        if args.infer_delay:
            # Widens the window a concurrency test needs: without it, a
            # local reply is fast enough that a WAV-write race would never
            # actually land inside another turn's request/response window.
            time.sleep(args.infer_delay)
        emit(f"OK language=english text={args.stub_text}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
