#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Characters the board's daemon breaks on the wire, put back together.

The daemon sends one character per SSE event. When a character's UTF-8
bytes straddle two model tokens, it still sends the character's full
length in the first event -- the bytes it has, then one it read past them
-- and the real last byte alone in the next. Captured on the N1-655 on
2026-10-07 (probe_meeting_summary.py --garbling):

    年 e5 b9 b4   arrived as   e5 b9 6e | b4
    完 e5 ae 8c   arrived as   e5 ae e9 | 8c

and on the 38-minute run, where the token held only the lead byte and
both bytes read past it happened to look like continuation bytes:

    调 e8 b0 83   arrived as   e8 9f e8 | b0 | 83
                               e8 82 e5 | b0 | 83

Decoded as it arrived, each became U+FFFD; joining the events' bytes did
not help either, because of the bytes in the middle. The bytes here are
those, verbatim.
"""

import pytest

from ambarella_llm2_python.utf8_mend import Mender, mend

from .test_regressions import drain, make_client


def sse(*events):
    return [b"data: " + e + b"\n\n" for e in events] + [b"data: <DONE>\n\n"]


@pytest.mark.asyncio
async def test_a_character_the_daemon_read_past_is_put_back():
    events = sse(
        b"<NL>",
        b"<NL>",
        b"1",
        b".",
        b"<SP>",
        b"\xe5\xb9n",
        b"\xb4",
        b"\xe5\x85\xb3",
        b"\xe4\xb8\xb4",
        b"\xe8\xbf\x91",
    )

    text = "".join(await drain(make_client(response_format="sse"), events))

    assert text == "<NL><NL>1.<SP>年关临近"


@pytest.mark.asyncio
async def test_the_other_capture_is_put_back_too():
    events = sse(
        b"\xef\xbc\x9a",
        b"1",
        b".",
        b"<SP>",
        b"\xe5\xae\xe9",
        b"\x8c",
        b"\xe6\x88\x90",
    )

    text = "".join(await drain(make_client(response_format="sse"), events))

    assert text == "：1.<SP>完成"


@pytest.mark.asyncio
async def test_two_bytes_read_past_a_lone_lead_byte_are_dropped():
    # The first junk byte, 9f, looks like a continuation byte; only the two
    # orphans that follow say how many of the slot's bytes are real.
    events = sse(
        b"2", b".", b"<SP>", b"\xe8\x9f\xe8", b"\xb0", b"\x83", b"\xe6\x95\xb4"
    )

    text = "".join(await drain(make_client(response_format="sse"), events))

    assert text == "2.<SP>调整"


@pytest.mark.asyncio
async def test_the_second_capture_of_that_is_put_back_too():
    events = sse(b"\xe8\x82\xe5", b"\xb0", b"\x83", b"\xe6\x95\xb4")

    text = "".join(await drain(make_client(response_format="sse"), events))

    assert text == "调整"


@pytest.mark.asyncio
async def test_a_character_cut_cleanly_between_events_is_joined():
    events = sse(b"\xe5", b"\xb9\xb4", b"\xe5\x85\xb3")

    text = "".join(await drain(make_client(response_format="sse"), events))

    assert text == "年关"


@pytest.mark.asyncio
async def test_a_character_that_cannot_be_mended_still_shows():
    # The next event does not carry the missing byte: nothing is guessed,
    # the break stays visible, and what follows is untouched.
    events = sse(b"\xe5\xb9n", b"a", b"\xe5\x85\xb3")

    text = "".join(await drain(make_client(response_format="sse"), events))

    assert "�" in text
    assert text.endswith("a关")


def test_the_mender_on_its_own_takes_events_and_gives_utf8():
    # probe_meeting_summary.py loads this module by path and calls it on
    # bytes saved from the board; this is the contract it relies on. A
    # character is held until the next event that is not an orphan says
    # how many bytes it was missing.
    assert mend([b"\xe5\xb9n", b"\xb4"]).decode("utf-8") == "年"
    mender = Mender()
    assert mender.feed(b"\xe5\xae\xe9") == b""
    assert mender.feed(b"\x8c") == b""
    assert mender.feed(b"\xe6\x88\x90") == "完".encode()
    assert mender.flush() == "成".encode()
