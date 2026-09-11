#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Client for the Ambarella resident speech daemons.

Both asr_d and tts_d load a model into Vector Processor memory at startup,
print a readiness token, then answer one command per line on stdin with one
terminal line on stdout. EazyAI logs to the same stdout, so any line that is
not a terminal line is vendor noise and is skipped.

Imports nothing from ten_ai_base, so these tests run outside the container.
"""

import asyncio
from typing import Any, Callable, List, Optional


class DaemonError(RuntimeError):
    """The daemon failed to become ready, died, or stopped answering.

    An `ERR ...` line in reply to a command is *not* this: it is a normal
    return value from `request()`, because `ERR no speech.` is how asr_d
    reports silence. Only load failures, death and timeouts raise.
    """


def _is_terminal(line: str) -> bool:
    return line.startswith("OK") or line.startswith("ERR")


class DaemonClient:
    """Owns one daemon child process and its request/response pipe."""

    def __init__(
        self,
        bin_path: str,
        flags: List[str],
        ready_token: str,
        logger: Any,
        load_timeout_s: float = 180.0,
        quit_timeout_s: float = 5.0,
        log_category: str = "vendor",
    ) -> None:
        self._bin_path = bin_path
        self._flags = list(flags)
        self._ready_token = ready_token
        self._log = logger
        self._load_timeout_s = load_timeout_s
        self._quit_timeout_s = quit_timeout_s
        self._log_category = log_category
        self._proc: Optional[
            asyncio.subprocess.Process  # pylint: disable=no-member
        ] = None
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._start_error: Optional[DaemonError] = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self.alive

    async def start(self) -> str:
        """Spawn the daemon and wait for its readiness token.

        Callers run this as a background task: the model load takes tens of
        seconds and must not block session setup.
        """
        self._ready.clear()
        self._start_error = None
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._bin_path,
                *self._flags,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as err:
            self._start_error = DaemonError(
                f"could not start {self._bin_path}: {err}"
            )
            # A waiter already blocked in wait_ready() must be woken now,
            # not after its own timeout expires.
            self._ready.set()
            raise self._start_error from err
        try:
            line = await asyncio.wait_for(
                self._read_terminal(
                    lambda text: text.startswith(self._ready_token),
                    raise_on_err=True,
                ),
                self._load_timeout_s,
            )
        except asyncio.TimeoutError as err:
            self._start_error = DaemonError(
                f"{self._bin_path} did not print {self._ready_token!r} "
                f"within {self._load_timeout_s}s"
            )
            await self._kill()
            self._ready.set()
            raise self._start_error from err
        except DaemonError as err:
            self._start_error = err
            await self._kill()
            self._ready.set()
            raise
        self._ready.set()
        return line

    async def wait_ready(self, timeout: float) -> None:
        """Block until the background start() has finished, or re-raise it."""
        if self._start_error is not None:
            raise self._start_error
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError as err:
            if self._start_error is not None:
                raise self._start_error from err
            raise DaemonError(
                f"{self._bin_path} was not ready within {timeout}s"
            ) from err
        if self._start_error is not None:
            raise self._start_error

    async def request(self, command: str, timeout: float) -> str:
        """Send one command and return its terminal line."""
        async with self._lock:
            if not self.alive:
                raise DaemonError(f"{self._bin_path} is not running")
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write((command + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            try:
                return await asyncio.wait_for(
                    self._read_terminal(_is_terminal, raise_on_err=False),
                    timeout,
                )
            except asyncio.TimeoutError as err:
                verb = command.split(" ", 1)[0]
                raise DaemonError(
                    f"{self._bin_path} did not answer {verb} "
                    f"within {timeout}s"
                ) from err

    async def stop(self) -> None:
        """Send QUIT and reap the process, so the VP memory is released."""
        if not self.alive:
            self._proc = None
            self._ready.clear()
            return
        assert self._proc is not None
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.write(b"QUIT\n")
                await self._proc.stdin.drain()
            await asyncio.wait_for(self._proc.wait(), self._quit_timeout_s)
        except (
            asyncio.TimeoutError,
            BrokenPipeError,
            ConnectionResetError,
        ):
            await self._kill()
        finally:
            self._proc = None
            self._ready.clear()

    async def _read_line(self) -> str:
        assert self._proc is not None and self._proc.stdout is not None
        raw = await self._proc.stdout.readline()
        if not raw:
            raise DaemonError(
                f"{self._bin_path} exited "
                f"(returncode={self._proc.returncode})"
            )
        return raw.decode("utf-8", errors="replace").strip()

    async def _read_terminal(
        self, is_terminal: Callable[[str], bool], raise_on_err: bool
    ) -> str:
        while True:
            line = await self._read_line()
            if not line:
                continue
            if is_terminal(line):
                return line
            if raise_on_err and line.startswith("ERR"):
                raise DaemonError(line)
            self._log.log_debug(
                f"{self._ready_token}: {line}",
                category=self._log_category,
            )

    async def _kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            try:
                await asyncio.wait_for(self._proc.wait(), 5.0)
            except asyncio.TimeoutError:
                pass
