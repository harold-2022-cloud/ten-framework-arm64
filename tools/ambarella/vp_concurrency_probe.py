#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Measure whether the board's three on-device models can infer together.

test_alternate.py in the vendor demo proves asr_d and tts_d can be resident
together and infer alternately. It never overlaps them, and it never involves
the LLM at all. The voice-assistant-ambarella graph runs all three on the same
Vector Processor:

    ambarella_asr_python  -> asr_d          (whisper tiny)
    ambarella_llm2_python -> test_llm       (deepseek_7B, over HTTP)
    ambarella_tts_python  -> tts_d          (openvoice)

Three overlaps occur in that pipeline, and only the first is rare:

    llm + tts   every turn -- main_control sends each finished sentence to TTS
                while the LLM is still streaming the next one
    asr + tts   barge-in while the agent is speaking
    asr + llm   barge-in while the agent is thinking

Run this on the board before trusting any of them. Residency is checked first,
because if the three models cannot be co-resident at all then no amount of
scheduling saves the design.

Deliberately standalone: this script imports nothing from the extensions, so a
failure here implicates the hardware and the vendor binaries, not the
extensions' Python.

Usage:
  python3 vp_concurrency_probe.py \\
      --asr-bin  /home/lychee/asr_tts_demo/app_demo/asr_d \\
      --tts-bin  /home/lychee/asr_tts_demo/app_demo/tts_d \\
      --whisper  /home/lychee/asr_tts_demo/n1-655_whisper_tiny \\
      --openvoice /home/lychee/asr_tts_demo/n1-655_openvoice \\
      --wav      /home/lychee/asr_tts_demo/app_demo/sample_16k_mono.wav \\
      --rounds   5

Exit code is 0 only when every model loaded, every phase ran to completion, and
no overlap produced a failure, so a runbook can gate on this script.
"""

import argparse
import asyncio
import json
import statistics
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple, Union

Process = asyncio.subprocess.Process  # pylint: disable=no-member
InferResult = Tuple[float, str]

# The board parses Session-Id numerically and refuses a zero, allows one user
# at a time, and holds a used session for 180s before freeing it. One id for
# the whole probe therefore keeps every request inside a single user's budget.
LLM_SESSION_ID = "20260914"


async def spawn(
    binary: str, flags: List[str], token: str, load_timeout_s: float
) -> Process:
    """Start one daemon and block until it prints its readiness token."""
    proc = await asyncio.create_subprocess_exec(
        binary,
        *flags,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        try:
            await asyncio.wait_for(
                _wait_for_token(proc, binary, token), load_timeout_s
            )
        except asyncio.TimeoutError as err:
            raise RuntimeError(
                f"load: {binary} did not print {token!r} within "
                f"{load_timeout_s}s"
            ) from err
        return proc
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


async def _wait_for_token(proc: Process, binary: str, token: str) -> None:
    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            raise RuntimeError(f"{binary} exited before printing {token}")
        line = raw.decode("utf-8", errors="replace").strip()
        if line.startswith(token):
            return
        if line.startswith("ERR"):
            raise RuntimeError(f"{binary}: {line}")
        if line:
            print(f"  [{token}] {line}")


async def infer(
    proc: Process, command: str, label: str, infer_timeout_s: float
) -> InferResult:
    """Send one command and time its terminal OK/ERR reply line.

    VP contention wedging a daemon mid-overlap is exactly the failure this
    probe exists to detect, so this must time out the same way the
    production DaemonClient does, rather than hang forever on a probe that
    is supposed to gate a runbook. `label` names both the phase and the
    daemon (e.g. "overlapped tts"), so a timeout says exactly where it
    happened.
    """
    assert proc.stdin is not None and proc.stdout is not None
    started = time.monotonic()
    proc.stdin.write((command + "\n").encode("utf-8"))
    await proc.stdin.drain()
    try:
        return await asyncio.wait_for(
            _read_terminal_line(proc, started), infer_timeout_s
        )
    except asyncio.TimeoutError as err:
        raise RuntimeError(
            f"{label} daemon did not answer within {infer_timeout_s}s "
            "during inference"
        ) from err


def _llm_request(
    url: str, model_type: int, prompt: str, timeout_s: float
) -> Tuple[bool, str]:
    """POST one prompt and reassemble the streamed reply. Blocking on purpose.

    Called through asyncio.to_thread so it can run while a daemon inference is
    in flight, which is the whole point of the overlapped phases.

    The board streams one character per SSE event and escapes whitespace, so
    the reply has to be joined before it means anything -- a substring search
    over the raw bytes finds nothing even when the text is there.
    """
    req = urllib.request.Request(
        url,
        data=prompt.encode("utf-8"),
        method="POST",
        headers={
            "Session-Id": LLM_SESSION_ID,
            "Model-Type": str(model_type),
            "Stream-Off": "0",
            "Reset-En": "1",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as err:
        # A refusal closes the connection with no HTTP response at all, so
        # this is what "the VP was busy" looks like from the client side.
        return False, f"{type(err).__name__}: {err}"

    joined = "".join(
        line[len("data:") :].lstrip(" ")
        for line in body.split("\n")
        if line.startswith("data:")
    )
    text = joined.replace("<SP>", " ").replace("<NL>", "\n")
    if "<DONE>" not in text:
        return False, "reply did not terminate with <DONE>"
    text = text.replace("<DONE>", "")
    _, tag, answer = text.partition("</think>")
    return True, (answer if tag else text).strip()


async def llm_infer(
    url: str,
    model_type: int,
    prompt: str,
    label: str,
    timeout_s: float,
) -> InferResult:
    """One LLM turn. `label` names the phase, so a failure says where."""
    started = time.monotonic()
    ok, detail = await asyncio.to_thread(
        _llm_request, url, model_type, prompt, timeout_s
    )
    elapsed = time.monotonic() - started
    if ok:
        return elapsed, "OK " + detail[:60]
    return elapsed, f"ERR [{label}] " + detail[:120]


async def _read_terminal_line(proc: Process, started: float) -> InferResult:
    assert proc.stdout is not None
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
        default=5,
        help="asr/tts rounds per phase, default 5",
    )
    parser.add_argument(
        "--llm-url",
        default="http://127.0.0.1:8080",
        help="the board's LLM demo server, default http://127.0.0.1:8080",
    )
    parser.add_argument(
        "--model-type",
        type=int,
        default=9,
        help="run_llm_demo.sh --model_type, default 9 (deepseek_7B)",
    )
    parser.add_argument(
        "--llm-rounds",
        type=int,
        default=2,
        help="rounds per phase that involve the LLM, default 2. Kept small "
        "because one generation takes 5-14s on an N1-655 and the board "
        "serves one session at a time",
    )
    parser.add_argument(
        "--llm-timeout-s",
        type=float,
        default=120.0,
        help="seconds to wait for one LLM generation, default 120.0. A turn "
        "takes 5-14s alone on an N1-655; the headroom is for contention",
    )
    parser.add_argument(
        "--llm-probe-timeout-s",
        type=float,
        default=30.0,
        help="seconds to wait for the residency check, default 30.0. Short on "
        "purpose: it only asks whether the server answers at all",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="probe asr_d and tts_d only, for a board where the LLM demo is "
        "not running. The three-way phase is then skipped entirely",
    )
    parser.add_argument(
        "--load-timeout-s",
        type=float,
        default=180.0,
        help="seconds to wait for each daemon's ready token, default 180.0 "
        "(matches the production extensions' load_timeout_s)",
    )
    parser.add_argument(
        "--infer-timeout-s",
        type=float,
        default=30.0,
        help="seconds to wait for one INFER's terminal reply, default 30.0 "
        "(matches the production extensions' infer_timeout_s)",
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
    failures: List[str] = []
    timing: Dict[str, List[float]] = {}

    def record(
        key: str, elapsed: float, line: str, fatal_err: bool = True
    ) -> None:
        timing.setdefault(key, []).append(elapsed)
        # asr_d answers "ERR no speech." for silence, which is a correct
        # answer about the audio rather than a failure of the VP.
        if line.startswith("ERR") and not (
            not fatal_err and line == "ERR no speech."
        ):
            failures.append(f"{key}: {line}")

    def tts_cmd(index: int) -> str:
        # Identical wording in every phase, so a text-length difference never
        # enters the baseline-versus-overlapped comparison.
        return f"INFER probe round {index} /tmp/probe_{index}.wav"

    asr_cmd = f"INFER {args.wav}"
    llm_url = args.llm_url.rstrip("/") + "/"

    # ---------------------------------------------------------------- phase 1
    print("=== 1. Residency")
    print("  the LLM is expected to be already resident, served over HTTP")
    # Fail fast here. This request only asks whether the server answers at
    # all, and waiting a full generation timeout to learn that it does not is
    # the difference between a useful diagnostic and a stalled one.
    elapsed, line = await llm_infer(
        llm_url, args.model_type, "Hi", "residency", args.llm_probe_timeout_s
    )
    if line.startswith("ERR"):
        print(f"  llm: {line}")
        print("\n  The LLM server did not answer, so nothing below would mean")
        print("  anything. Start it, or pass --skip-llm to probe asr+tts only.")
        if not args.skip_llm:
            return 2
    else:
        print(f"  llm resident and answering ({elapsed:.1f}s)")

    print("  loading asr_d ...")
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
        args.load_timeout_s,
    )
    print("  loading tts_d ...")
    daemons["tts"] = await spawn(
        args.tts_bin,
        ["--model_dir", args.openvoice, "--speaker_id", "0", "--log", "1"],
        "READY tts",
        args.load_timeout_s,
    )
    print("  all three models are co-resident\n")
    asr, tts = daemons["asr"], daemons["tts"]

    # ---------------------------------------------------------------- phase 2
    print(f"=== 2. Baseline, {args.rounds} rounds each, nothing overlapping")
    for index in range(args.rounds):
        record(
            "asr alone",
            *await infer(asr, asr_cmd, "baseline asr", args.infer_timeout_s),
            fatal_err=False,
        )
        record(
            "tts alone",
            *await infer(
                tts, tts_cmd(index), "baseline tts", args.infer_timeout_s
            ),
        )
    if not args.skip_llm:
        for index in range(args.llm_rounds):
            record(
                "llm alone",
                *await llm_infer(
                    llm_url,
                    args.model_type,
                    f"Say hello, attempt {index}.",
                    "baseline llm",
                    args.llm_timeout_s,
                ),
            )
    print("  done\n")

    # ---------------------------------------------------------------- phase 3
    async def overlap(phase: str, *coros) -> None:
        """Run several inferences together and file each under this phase.

        Rows are named "<model> in <phase>" rather than "<model> with
        <partner>", which would give a model its own name as its partner.
        """
        results = await asyncio.gather(*coros, return_exceptions=True)
        for name, res in results:
            if isinstance(res, BaseException):
                failures.append(f"{phase} {name}: {type(res).__name__}: {res}")
            else:
                record(f"{name} in {phase}", *res, fatal_err=(name != "asr"))

    async def tagged(name: str, coro):
        try:
            return name, await coro
        except Exception as err:  # noqa: BLE001 - recorded, not raised
            return name, err

    print("=== 3. Pairwise overlap")
    if not args.skip_llm:
        print("  llm + tts   (happens on every turn of a real conversation)")
        for index in range(args.llm_rounds):
            await overlap(
                "llm+tts",
                tagged(
                    "llm",
                    llm_infer(
                        llm_url,
                        args.model_type,
                        f"Count to five, attempt {index}.",
                        "overlap llm",
                        args.llm_timeout_s,
                    ),
                ),
                tagged(
                    "tts",
                    infer(
                        tts,
                        tts_cmd(100 + index),
                        "overlap tts",
                        args.infer_timeout_s,
                    ),
                ),
            )

    print("  asr + tts   (barge-in while the agent is speaking)")
    for index in range(args.rounds):
        await overlap(
            "asr+tts",
            tagged(
                "asr", infer(asr, asr_cmd, "overlap asr", args.infer_timeout_s)
            ),
            tagged(
                "tts",
                infer(
                    tts,
                    tts_cmd(200 + index),
                    "overlap tts",
                    args.infer_timeout_s,
                ),
            ),
        )

    if not args.skip_llm:
        print("  asr + llm   (barge-in while the agent is thinking)")
        for index in range(args.llm_rounds):
            await overlap(
                "asr+llm",
                tagged(
                    "llm",
                    llm_infer(
                        llm_url,
                        args.model_type,
                        f"Name three colours, attempt {index}.",
                        "overlap llm",
                        args.llm_timeout_s,
                    ),
                ),
                tagged(
                    "asr",
                    infer(asr, asr_cmd, "overlap asr", args.infer_timeout_s),
                ),
            )

    # ---------------------------------------------------------------- phase 4
    if not args.skip_llm:
        print("\n=== 4. All three at once")
        for index in range(args.llm_rounds):
            await overlap(
                "all three",
                tagged(
                    "llm",
                    llm_infer(
                        llm_url,
                        args.model_type,
                        f"Describe the sky, attempt {index}.",
                        "three-way llm",
                        args.llm_timeout_s,
                    ),
                ),
                tagged(
                    "asr",
                    infer(asr, asr_cmd, "three-way asr", args.infer_timeout_s),
                ),
                tagged(
                    "tts",
                    infer(
                        tts,
                        tts_cmd(300 + index),
                        "three-way tts",
                        args.infer_timeout_s,
                    ),
                ),
            )

    # ---------------------------------------------------------------- phase 5
    print("\n=== 5. Results")
    for key in sorted(timing):
        report(key, timing[key])

    def ratio(under: str, alone: str) -> Optional[float]:
        a, b = timing.get(under), timing.get(alone)
        if not a or not b:
            return None
        return statistics.median(a) / statistics.median(b)

    print("\n  slowdown versus running alone")
    for under, alone, what in (
        ("tts in llm+tts", "tts alone", "tts, while the llm generates"),
        ("llm in llm+tts", "llm alone", "llm, while tts synthesises"),
        ("asr in asr+tts", "asr alone", "asr, while tts synthesises"),
        ("tts in asr+tts", "tts alone", "tts, while asr transcribes"),
        ("asr in asr+llm", "asr alone", "asr, while the llm generates"),
        ("llm in asr+llm", "llm alone", "llm, while asr transcribes"),
        ("asr in all three", "asr alone", "asr, with both others running"),
        ("tts in all three", "tts alone", "tts, with both others running"),
        ("llm in all three", "llm alone", "llm, with both others running"),
    ):
        value = ratio(under, alone)
        if value is not None:
            print(f"    {what:38} {value:.2f}x")

    print("\n=== Verdict")
    if failures:
        for failure in failures[:20]:
            print(f"  FAIL {failure}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")
        print(
            "\n  Overlapping inference is NOT safe on this board as configured."
            "\n  The all-Ambarella graph would have to serialise the models."
        )
        return 1

    print(
        "  Every model stayed up and every overlap completed."
        "\n  Read the slowdown figures above: they are the cost of the"
        "\n  overlaps, not a reason to avoid them."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
