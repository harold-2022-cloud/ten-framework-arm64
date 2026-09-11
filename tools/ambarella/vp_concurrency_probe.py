#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Measure whether asr_d and tts_d can infer at the same time.

test_alternate.py in the vendor demo proves the two daemons can be resident
together and infer alternately. It never overlaps them. A voice pipeline does:
a user barging in while TTS is synthesising puts an ASR INFER on top of a TTS
INFER. Run this on the board before trusting that path.

Deliberately standalone: this script imports nothing from the extensions, so
a failure here implicates the hardware and the vendor binaries, not the
extensions' Python.

Usage:
  python3 vp_concurrency_probe.py \\
      --asr-bin  /home/lychee/asr_tts_demo/app_demo/asr_d \\
      --tts-bin  /home/lychee/asr_tts_demo/app_demo/tts_d \\
      --whisper  /home/lychee/asr_tts_demo/n1-655_whisper_tiny \\
      --openvoice /home/lychee/asr_tts_demo/n1-655_openvoice \\
      --wav      /home/lychee/asr_tts_demo/app_demo/sample_16k_mono.wav \\
      --rounds   10

Exit code is 0 only when both daemons came up, ran to completion, and
overlapped inference produced no failures. Anything else -- a daemon that
never got ready, one that dies mid-run, or an ERR that is not the expected
"ERR no speech." -- is a non-zero exit, so a runbook can gate on this script.
"""

import argparse
import asyncio
import statistics
import time
from typing import Dict, List, Optional, Tuple, Union

Process = asyncio.subprocess.Process  # pylint: disable=no-member
InferResult = Tuple[float, str]


async def spawn(binary: str, flags: List[str], token: str) -> Process:
    """Start one daemon and block until it prints its readiness token."""
    proc = await asyncio.create_subprocess_exec(
        binary,
        *flags,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                raise RuntimeError(f"{binary} exited before printing {token}")
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith(token):
                return proc
            if line.startswith("ERR"):
                raise RuntimeError(f"{binary}: {line}")
            if line:
                print(f"  [{token}] {line}")
    except Exception:
        # Kill process before re-raising to ensure VP memory is released.
        # Per vendor docs, only QUIT (or kill) releases VP memory.
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 5.0)
            except asyncio.TimeoutError:
                pass
        raise


async def infer(proc: Process, command: str) -> InferResult:
    """Send one command and time its terminal OK/ERR reply line."""
    assert proc.stdin is not None and proc.stdout is not None
    started = time.monotonic()
    proc.stdin.write((command + "\n").encode("utf-8"))
    await proc.stdin.drain()
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            raise RuntimeError("daemon exited mid-inference")
        line = raw.decode("utf-8", errors="replace").strip()
        if line.startswith("OK") or line.startswith("ERR"):
            return time.monotonic() - started, line


async def quit_daemon(proc: Process) -> None:
    """Ask a daemon to exit and reap it, tolerating one already gone.

    Called from cleanup, possibly after a crash or a half-finished startup,
    so this must never itself raise -- a broken pipe here just means the
    daemon is already dead, which is fine; kill() is a no-op on a dead pid.
    """
    if proc.returncode is not None:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.write(b"QUIT\n")
            await proc.stdin.drain()
        await asyncio.wait_for(proc.wait(), 10.0)
    except (
        asyncio.TimeoutError,
        BrokenPipeError,
        ConnectionResetError,
        ProcessLookupError,
    ):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 5.0)
        except asyncio.TimeoutError:
            pass


def report(label: str, samples: List[float]) -> None:
    if not samples:
        print(f"  {label:26} n=0   (no samples collected)")
        return
    print(
        f"  {label:26} n={len(samples):<3} "
        f"mean={statistics.mean(samples) * 1000:7.1f} ms  "
        f"max={max(samples) * 1000:7.1f} ms"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--asr-bin", required=True, help="path to asr_d")
    parser.add_argument("--tts-bin", required=True, help="path to tts_d")
    parser.add_argument(
        "--whisper", required=True, help="asr_d --cavalry_dir model dir"
    )
    parser.add_argument(
        "--openvoice", required=True, help="tts_d --model_dir model dir"
    )
    parser.add_argument(
        "--wav", required=True, help="16k mono sample WAV to feed asr_d"
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=10,
        help="rounds per phase (baseline and overlapped), default 10",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()

    # Populated as each daemon actually comes up, so the finally block below
    # reaps exactly the processes that exist -- including just one of them,
    # if the other failed to spawn or never printed its ready token. This is
    # the one part of the script that must run on every exit path: VP memory
    # is only released by QUIT (or, failing that, kill()), never by the
    # Python process simply exiting.
    daemons: Dict[str, Process] = {}
    exit_code = 1

    try:
        try:
            exit_code = await run_probe(args, daemons)
        except (RuntimeError, OSError) as err:
            print(f"\nERROR: {err}")
            exit_code = 1
    finally:
        for name, proc in daemons.items():
            try:
                await quit_daemon(proc)
            except Exception as err:  # pylint: disable=broad-except
                # Cleanup must not mask whatever main() was already failing
                # on, and one daemon's cleanup failing must not skip the
                # other's.
                print(f"  warning: failed to clean up {name} daemon: {err}")

    return exit_code


async def run_probe(
    args: argparse.Namespace, daemons: Dict[str, Process]
) -> int:
    print("Loading both daemons into VP memory...")
    daemons["asr"] = await spawn(
        args.asr_bin,
        [
            "--cavalry_dir",
            args.whisper,
            "--type",
            "tiny",
            "--language",
            "english",
            "--log",
            "1",
        ],
        "READY asr",
    )
    daemons["tts"] = await spawn(
        args.tts_bin,
        ["--model_dir", args.openvoice, "--speaker_id", "0", "--log", "1"],
        "READY tts",
    )
    print("Both resident.\n")
    asr, tts = daemons["asr"], daemons["tts"]

    seq_asr: List[float] = []
    seq_tts: List[float] = []
    con_asr: List[float] = []
    con_tts: List[float] = []
    failures: List[str] = []

    print(f"Baseline: {args.rounds} alternating rounds")
    for index in range(args.rounds):
        elapsed, line = await infer(asr, f"INFER {args.wav}")
        seq_asr.append(elapsed)
        if line.startswith("ERR") and line != "ERR no speech.":
            failures.append(f"sequential asr round {index}: {line}")
        elapsed, line = await infer(
            tts, f"INFER baseline round {index} /tmp/probe_seq_{index}.wav"
        )
        seq_tts.append(elapsed)
        if line.startswith("ERR"):
            failures.append(f"sequential tts round {index}: {line}")

    print(f"\nOverlapped: {args.rounds} simultaneous rounds")
    for index in range(args.rounds):
        asr_task = asyncio.create_task(infer(asr, f"INFER {args.wav}"))
        tts_task = asyncio.create_task(
            infer(
                tts,
                f"INFER overlapped round {index} /tmp/probe_con_{index}.wav",
            )
        )
        # return_exceptions=True: a daemon dying mid-round must not abort
        # the gather while leaving the *other* task's result (and therefore
        # its process) unaccounted for -- both outcomes are inspected below,
        # and either one can be an exception without losing the other.
        results: List[Union[InferResult, BaseException]] = list(
            await asyncio.gather(asr_task, tts_task, return_exceptions=True)
        )
        asr_result, tts_result = results
        stop_early = False

        if isinstance(asr_result, BaseException):
            failures.append(f"concurrent asr round {index}: {asr_result}")
            stop_early = True
        else:
            asr_ms, asr_line = asr_result
            con_asr.append(asr_ms)
            if asr_line.startswith("ERR") and asr_line != "ERR no speech.":
                failures.append(f"concurrent asr round {index}: {asr_line}")

        if isinstance(tts_result, BaseException):
            failures.append(f"concurrent tts round {index}: {tts_result}")
            stop_early = True
        else:
            tts_ms, tts_line = tts_result
            con_tts.append(tts_ms)
            if tts_line.startswith("ERR"):
                failures.append(f"concurrent tts round {index}: {tts_line}")

        if stop_early:
            print(f"  round {index}: a daemon exited mid-run, stopping")
            break

    print("\nResults")
    report("asr, alternating", seq_asr)
    report("asr, overlapped", con_asr)
    report("tts, alternating", seq_tts)
    report("tts, overlapped", con_tts)

    asr_ratio: Optional[float] = None
    tts_ratio: Optional[float] = None
    if seq_asr and con_asr:
        asr_ratio = statistics.mean(con_asr) / statistics.mean(seq_asr)
        print(f"\n  asr slowdown when overlapped: {asr_ratio:.2f}x")
    if seq_tts and con_tts:
        tts_ratio = statistics.mean(con_tts) / statistics.mean(seq_tts)
        print(f"  tts slowdown when overlapped: {tts_ratio:.2f}x")

    print("\nVerdict")
    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        print(
            "  Concurrent inference FAILS on this board. Serialise VP "
            "access with a module-level asyncio.Lock shared by both "
            "extensions, and record the added barge-in latency."
        )
        return 1

    if asr_ratio is None or tts_ratio is None:
        print("  Not enough samples were collected to reach a verdict.")
        return 1

    # 2x is the threshold this probe treats as "cheap": below it, overlapped
    # inference is judged close enough to the alternating baseline that a
    # barge-in would not read as a stall. It is a judgement call, not a
    # measured cliff -- record the actual ratios in the spec regardless, so
    # a reviewer can re-draw the line themselves.
    if max(asr_ratio, tts_ratio) > 2.0:
        print(
            "  Concurrent inference works but costs more than 2x versus "
            "alternating. Consider serialising, and measure the barge-in "
            "latency either way."
        )
        return 0

    print(
        "  Concurrent inference is safe and cheap on this board. No "
        "cross-extension lock is needed; spec section 9 is resolved."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
