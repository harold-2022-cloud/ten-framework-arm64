#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The worker keeps itself alive while a meeting is being processed.

The Go server reaps a worker that has gone `timeout` seconds without a
/ping (worker_common.go:159), and an hour of meeting takes about 75 minutes
to process. So the uploader pings for it, and only while there is
something to finish: once the meeting is done the client's own timeout
applies again. A fake /ping here stands in for the Go server.
"""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from meeting_uploader.config import MeetingUploaderConfig
from meeting_uploader.keepalive import KeepAlive, ping_url


class GoServer:
    def __init__(self, code="0"):
        self.bodies = []
        self.code = code

    async def ping(self, request):
        self.bodies.append(await request.json())
        return web.json_response({"code": self.code, "msg": "", "data": None})

    def server(self):
        app = web.Application()
        app.router.add_post("/ping", self.ping)
        return TestServer(app)


async def run_for(keepalive, seconds=0.12):
    task = asyncio.create_task(keepalive.run())
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_while_busy_it_pings_with_its_channel():
    go = GoServer()
    async with go.server() as server:
        await run_for(
            KeepAlive(
                str(server.make_url("/ping")),
                "meeting-1",
                0.02,
                busy=lambda: True,
            )
        )

    assert len(go.bodies) >= 2
    assert {b["channel_name"] for b in go.bodies} == {"meeting-1"}
    assert all(b["request_id"] for b in go.bodies)


@pytest.mark.asyncio
async def test_while_idle_it_leaves_the_timeout_to_the_client():
    go = GoServer()
    async with go.server() as server:
        await run_for(
            KeepAlive(
                str(server.make_url("/ping")),
                "meeting-1",
                0.02,
                busy=lambda: False,
            )
        )

    assert go.bodies == []


@pytest.mark.asyncio
async def test_a_refused_ping_is_logged_and_the_next_one_still_goes():
    go = GoServer(code="10002")
    logged = []
    async with go.server() as server:
        await run_for(
            KeepAlive(
                str(server.make_url("/ping")),
                "meeting-1",
                0.02,
                busy=lambda: True,
                log=logged.append,
            )
        )

    assert len(go.bodies) >= 2
    assert "10002" in logged[0]


@pytest.mark.asyncio
async def test_an_unreachable_server_is_logged_not_raised():
    logged = []

    await run_for(
        KeepAlive(
            "http://127.0.0.1:9/ping",
            "meeting-1",
            0.02,
            busy=lambda: True,
            log=logged.append,
        )
    )

    assert len(logged) >= 2


@pytest.mark.asyncio
async def test_without_a_channel_it_says_so_and_stops():
    logged = []
    keepalive = KeepAlive(
        "http://127.0.0.1:9/ping",
        "",
        0.02,
        busy=lambda: True,
        log=logged.append,
    )

    await asyncio.wait_for(keepalive.run(), timeout=1.0)

    assert len(logged) == 1
    assert "channel" in logged[0]


def test_the_url_is_the_server_that_started_this_worker(monkeypatch):
    # The worker inherits the Go server's environment (worker_linux.go:31),
    # and on the board that server is on 8081: 8080 is the vendor's LLM.
    monkeypatch.setenv("SERVER_PORT", "8081")
    assert ping_url(MeetingUploaderConfig()) == "http://127.0.0.1:8081/ping"

    monkeypatch.delenv("SERVER_PORT")
    assert ping_url(MeetingUploaderConfig()) == "http://127.0.0.1:8080/ping"

    explicit = MeetingUploaderConfig(ping_url="http://10.0.0.2:9000/ping")
    assert ping_url(explicit) == "http://10.0.0.2:9000/ping"
