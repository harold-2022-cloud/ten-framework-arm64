#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Put back the characters the board's daemon breaks on the wire.

The daemon sends one character per SSE event. When a character's UTF-8
bytes straddle two model tokens, it sends the character's full length
anyway: the bytes the token has, then as many read past them as are
missing. The missing real bytes come after, each in an event of its own.
Captured on an N1-655 on 2026-10-07 (tools/ambarella/probe_meeting_summary.py
--garbling):

    年 e5 b9 b4   arrived as   e5 b9 6e | b4
    完 e5 ae 8c   arrived as   e5 ae e9 | 8c
    调 e8 b0 83   arrived as   e8 9f e8 | b0 | 83
                               e8 82 e5 | b0 | 83

The bytes read past the token are whatever was there, and can look like
continuation bytes (9f, 82 above), so they cannot be told apart by their
values. What tells is what follows: a well-formed stream never starts an
event with a continuation byte, so continuation bytes arriving on their
own -- orphans -- are the bytes the character before them is missing, and
their count is how many of its slot were read past the token. The
character is then its lead byte, the real bytes after it, and the orphans;
the rest of the slot is dropped.

So every event that is one multi-byte character is held until the next
event that is not an orphan, which costs one event of delay. A held
character no orphans follow goes out exactly as it came, broken or not:
nothing is ever guessed. A character cut cleanly at an event boundary --
no bytes read past -- is joined by the same rule.

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
    settled."""

    def __init__(self) -> None:
        self._slot = b""  # an event that is one multi-byte character
        self._orphans = bytearray()  # continuation bytes that followed it

    def feed(self, event: bytes) -> bytes:
        i = 0
        if self._slot:
            while i < len(event) and _continuation(event[i]):
                self._orphans.append(event[i])
                i += 1
            if event and i == len(event):
                return b""  # only orphans: more may follow
        out = self._settle()
        rest = event[i:]
        need = _length(rest[0]) if rest else None
        if need is not None and len(rest) <= 1 + need:
            self._slot = bytes(rest)  # one character: see what follows
        else:
            out += rest
        return out

    def flush(self) -> bytes:
        return self._settle()

    def _settle(self) -> bytes:
        slot, orphans = self._slot, bytes(self._orphans)
        self._slot, self._orphans = b"", bytearray()
        if not slot or not orphans:
            return slot + orphans
        need = _length(slot[0])
        keep = need - len(orphans)
        real = slot[1 : 1 + keep]
        if keep >= 0 and len(real) == keep and all(map(_continuation, real)):
            return slot[:1] + real + orphans
        return slot + orphans  # not the daemon's pattern: leave it broken


def mend(events: Iterable[bytes]) -> bytes:
    """Every event's bytes, joined and mended."""
    mender = Mender()
    out = b"".join(mender.feed(e) for e in events)
    return out + mender.flush()
