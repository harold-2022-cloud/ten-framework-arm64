#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Put back the characters the board's daemon breaks on the wire.

The daemon sends one character per SSE event. When a character's UTF-8
bytes straddle two model tokens, it sends the character's full length
anyway: the bytes it has, then whatever it read past them, and the real
remaining bytes alone in the next event. Captured on an N1-655 on
2026-10-07 (tools/ambarella/probe_meeting_summary.py --garbling):

    年 e5 b9 b4   arrived as   e5 b9 6e | b4
    完 e5 ae 8c   arrived as   e5 ae e9 | 8c

Nothing is lost, only displaced, and the extra byte can never be a
continuation byte where one is expected -- that is what makes it visible.
So: a character left short in one event, followed in that event by exactly
as many bytes as it is short, all of them not continuation bytes, is
completed from the next event if that event starts with the missing
continuation bytes; the bytes in between are dropped. A character cut
cleanly at an event boundary is joined the same way. Anything else is left
as it came, to decode as U+FFFD: a break the rule does not recognise is
shown, never guessed at.

Standard library only, so the probe can load this file by path.
"""

from typing import Iterable, Optional


def _continuation(byte: int) -> bool:
    return 0x80 <= byte <= 0xBF


def _length(lead: int) -> Optional[int]:
    """How many continuation bytes follow this lead byte; None if it is
    not one."""
    if 0xC2 <= lead <= 0xDF:
        return 1
    if 0xE0 <= lead <= 0xEF:
        return 2
    if 0xF0 <= lead <= 0xF4:
        return 3
    return None


class Mender:
    """Fed one event's bytes at a time; gives back the bytes that are
    settled. A character still waiting on the next event is held."""

    def __init__(self) -> None:
        self._held = b""  # a lead byte and the continuations it has
        self._missing = 0  # continuations it still needs

    def feed(self, event: bytes) -> bytes:
        out = bytearray()
        start = 0
        if self._missing:
            take = 0
            while (
                take < self._missing
                and take < len(event)
                and _continuation(event[take])
            ):
                take += 1
            if take == self._missing:
                out += self._held + event[:take]
                start = take
            else:
                out += self._held  # not completed here: let it show
            self._held, self._missing = b"", 0
        out += self._scan(event, start)
        return bytes(out)

    def flush(self) -> bytes:
        held, self._held, self._missing = self._held, b"", 0
        return held

    def _scan(self, event: bytes, i: int) -> bytes:
        out = bytearray()
        while i < len(event):
            need = _length(event[i])
            if need is None:
                out.append(event[i])
                i += 1
                continue
            have = 0
            while (
                have < need
                and i + 1 + have < len(event)
                and _continuation(event[i + 1 + have])
            ):
                have += 1
            if have == need:
                out += event[i : i + 1 + need]
                i += 1 + need
                continue
            short = need - have
            after = event[i + 1 + have :]
            if not after or (
                len(after) == short and not any(map(_continuation, after))
            ):
                # Cut at the boundary, or padded to full length with bytes
                # read past the token: wait for the rest in the next event.
                self._held = event[i : i + 1 + have]
                self._missing = short
                return bytes(out)
            out += event[i : i + 1 + have]  # unrecognised: leave it broken
            i += 1 + have
        return bytes(out)


def mend(events: Iterable[bytes]) -> bytes:
    """Every event's bytes, joined and mended."""
    mender = Mender()
    out = b"".join(mender.feed(e) for e in events)
    return out + mender.flush()
