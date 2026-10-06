#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Pinging the Go server for this worker while a meeting is in progress.

The server stops a worker that goes `timeout` seconds without a /ping
(worker_common.go:159), 60 by default, and an hour of meeting takes about
75 minutes to process. Leaving that to the client meant the client had to
size the timeout from the recording's length or stay online pinging; get
it wrong and the worker died mid-meeting with nothing said. So the worker
pings for itself, and only while it has work: once the meeting is done the
client's timeout governs again and an idle worker is still reaped.
"""

import asyncio
import os
import uuid
from typing import Callable

import aiohttp

from .config import MeetingUploaderConfig


def ping_url(config: MeetingUploaderConfig) -> str:
    """The server that started this worker: the worker inherits its
    environment (worker_linux.go:31), SERVER_PORT included. On the board
    that is 8081 -- 8080 is the vendor's LLM daemon."""
    if config.ping_url:
        return config.ping_url
    return f"http://127.0.0.1:{os.environ.get('SERVER_PORT') or 8080}/ping"


class KeepAlive:
    def __init__(
        self,
        url: str,
        channel: str,
        interval_s: float,
        busy: Callable[[], bool],
        log: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.url = url
        self.channel = channel
        self.interval_s = interval_s
        self.busy = busy
        self.log = log

    async def run(self) -> None:
        if not self.channel:
            # Started by hand rather than by the server's /start: there is
            # no worker entry to keep alive, and nothing would reap it.
            self.log("no channel was injected; not keeping the worker alive")
            return
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                await asyncio.sleep(self.interval_s)
                if self.busy():
                    await self._ping(session)

    async def _ping(self, session: aiohttp.ClientSession) -> None:
        body = {"request_id": str(uuid.uuid4()), "channel_name": self.channel}
        try:
            async with session.post(self.url, json=body) as response:
                reply = await response.json(content_type=None)
            # HTTP 200 is not success here; code "0" is (agent_api.zh-TW.md).
            if str(reply.get("code")) != "0":
                self.log(f"ping for {self.channel} refused: {reply}")
        except Exception as err:  # pylint: disable=broad-except
            self.log(f"ping to {self.url} failed: {err!r}")
