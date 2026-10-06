#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The uploader through the TEN runtime, the way the graph loads it.

The tester plays the controller -- it answers meeting_uploaded -- and the
client, uploading over real HTTP to the port the extension opened. Where
it matters it also plays the Go server's /ping.

    task test-extension EXTENSION=agents/ten_packages/extension/meeting_uploader
"""

import asyncio
import io
import json
import socket

import aiohttp
import numpy as np
import soundfile as sf
from aiohttp import web
from ten_runtime import (
    AsyncExtensionTester,
    AsyncTenEnvTester,
    Cmd,
    CmdResult,
    StatusCode,
)

from meeting_uploader import store

ADDON = "meeting_uploader"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ogg_bytes() -> bytes:
    t = np.arange(16000) / 16000
    buf = io.BytesIO()
    sf.write(
        buf,
        0.3 * np.sin(2 * np.pi * 440 * t),
        16000,
        format="OGG",
        subtype="OPUS",
    )
    return buf.getvalue()


AUDIO = ogg_bytes()


class Client(AsyncExtensionTester):
    def __init__(self, port: int, upload=True, controller_ok=True) -> None:
        super().__init__()
        self.base = f"http://127.0.0.1:{port}"
        self.upload = upload
        self.controller_ok = controller_ok
        self.uploaded = None
        self.http = None
        self.pings = []
        self.task = None

    async def on_start(self, ten_env: AsyncTenEnvTester) -> None:
        self.task = asyncio.create_task(self._drive(ten_env))

    async def on_cmd(self, ten_env: AsyncTenEnvTester, cmd: Cmd) -> None:
        if cmd.get_name() == "meeting_uploaded":
            payload, _ = cmd.get_property_to_json(None)
            self.uploaded = json.loads(payload)
        status = StatusCode.OK if self.controller_ok else StatusCode.ERROR
        await ten_env.return_result(CmdResult.create(status, cmd))

    async def _drive(self, ten_env: AsyncTenEnvTester) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                await self._wait_until_listening(session)
                if self.upload:
                    await self._upload(session)
                await self.after(session)
        finally:
            ten_env.stop_test()

    async def _wait_until_listening(self, session) -> None:
        # What a client does after /start: ask until the uploader answers.
        for _ in range(100):
            try:
                async with session.get(f"{self.base}/meetings") as r:
                    if r.status == 200:
                        return
            except aiohttp.ClientConnectionError:
                pass
            await asyncio.sleep(0.05)
        raise AssertionError("the uploader never started listening")

    async def _upload(self, session) -> None:
        form = aiohttp.FormData()
        form.add_field("meeting_id", "m1")
        form.add_field("speakers", "3")
        form.add_field("file", AUDIO, filename="meeting.ogg")
        async with session.post(f"{self.base}/meeting/upload", data=form) as r:
            self.http = (r.status, await r.json())

    async def after(self, session) -> None:
        pass


class PingedClient(Client):
    """Also the Go server: answers /ping and waits for one."""

    def __init__(self, port: int, ping_port: int) -> None:
        super().__init__(port)
        self.ping_port = ping_port

    async def _drive(self, ten_env: AsyncTenEnvTester) -> None:
        app = web.Application()
        app.router.add_post("/ping", self._ping)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.ping_port).start()
        try:
            await super()._drive(ten_env)
        finally:
            await runner.cleanup()

    async def _ping(self, request):
        self.pings.append(await request.json())
        return web.json_response({"code": "0", "msg": "success", "data": None})

    async def after(self, session) -> None:
        for _ in range(100):
            if self.pings:
                return
            await asyncio.sleep(0.05)


def run(tester: Client, **properties) -> Client:
    tester.set_test_mode_single(ADDON, json.dumps(properties))
    err = tester.run()
    assert err is None, err.error_message()
    return tester


def test_an_upload_lands_and_reaches_the_controller(tmp_path):
    port = free_port()
    tester = run(Client(port), listen_port=port, meetings_dir=str(tmp_path))

    assert tester.http[0] == 200
    assert tester.uploaded["ogg_path"] == str(tmp_path / "m1" / "audio.ogg")
    assert tester.uploaded["work_dir"] == str(tmp_path / "m1" / "work")
    assert tester.uploaded["speakers"] == 3
    assert (tmp_path / "m1" / "audio.ogg").read_bytes() == AUDIO


def test_a_controller_that_refuses_fails_the_meeting(tmp_path):
    port = free_port()
    tester = run(
        Client(port, controller_ok=False),
        listen_port=port,
        meetings_dir=str(tmp_path),
    )

    assert tester.http[0] == 503
    assert store.read_state(str(tmp_path / "m1"))["state"] == "failed"


def test_while_a_meeting_is_processed_the_worker_pings_its_server(tmp_path):
    port, ping_port = free_port(), free_port()
    tester = run(
        PingedClient(port, ping_port),
        listen_port=port,
        meetings_dir=str(tmp_path),
        channel="meeting-1",
        ping_url=f"http://127.0.0.1:{ping_port}/ping",
        keepalive_s=0.05,
    )

    assert tester.pings
    assert tester.pings[0]["channel_name"] == "meeting-1"


def test_a_meeting_left_half_done_by_the_last_worker_is_marked_failed(
    tmp_path,
):
    folder = tmp_path / "old"
    (folder / "work").mkdir(parents=True)
    store.write_state(str(folder), state="transcribing", topics_done=2)
    port = free_port()

    run(
        Client(port, upload=False), listen_port=port, meetings_dir=str(tmp_path)
    )

    state = store.read_state(str(folder))
    assert state["state"] == "failed"
    assert state["topics_done"] == 2
