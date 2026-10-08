#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The TEN shell: open the HTTP port, hand landed meetings on, keep alive.

Everything with behaviour lives in store.py, server.py and keepalive.py;
this file only wires them to the runtime. Callbacks catch what they raise:
an exception escaping one makes the runtime os._exit(1), and the worker
would vanish with a meeting half done.
"""

import asyncio
import json
from typing import Optional

from aiohttp import web
from ten_runtime import AsyncExtension, AsyncTenEnv, Cmd, StatusCode

from . import store
from .config import MeetingUploaderConfig
from .keepalive import KeepAlive, ping_url
from .server import UPLOADS, make_app


class MeetingUploaderExtension(AsyncExtension):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.ten_env: Optional[AsyncTenEnv] = None
        self.config = MeetingUploaderConfig()
        self.runner: Optional[web.AppRunner] = None
        self.keepalive: Optional[asyncio.Task] = None

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        self.ten_env = ten_env
        config_json, _ = await ten_env.get_property_to_json("")
        self.config = MeetingUploaderConfig.model_validate_json(
            config_json or "{}"
        )

    async def on_start(self, ten_env: AsyncTenEnv) -> None:
        try:
            await self._start(ten_env)
        except Exception as err:  # pylint: disable=broad-except
            ten_env.log_error(f"meeting uploader did not start: {err!r}")

    async def _start(self, ten_env: AsyncTenEnv) -> None:
        config = self.config
        app = make_app(config, self._emit, log=ten_env.log_info)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        try:
            await web.TCPSite(
                self.runner, "0.0.0.0", config.listen_port
            ).start()
        except OSError as err:
            # Two meeting workers cannot share the port; the second one
            # would take uploads nobody listens for.
            ten_env.log_error(
                f"cannot listen on {config.listen_port} ({err}); "
                "is another meeting worker running?"
            )
            return
        # Only the worker that holds the port may say a meeting was left
        # half done: until then another worker may be in the middle of it.
        stopped = store.fail_interrupted(config.meetings_dir)
        if stopped:
            ten_env.log_warn(f"left unfinished by the last worker: {stopped}")
        ten_env.log_info(
            f"meeting uploads on :{config.listen_port} into "
            f"{config.meetings_dir}"
            + (" (token required)" if config.auth_token else "")
        )

        keepalive = KeepAlive(
            ping_url(config),
            config.channel,
            config.keepalive_s,
            busy=app[UPLOADS].busy,
            log=ten_env.log_warn,
        )
        self.keepalive = asyncio.create_task(keepalive.run())

    async def on_stop(self, ten_env: AsyncTenEnv) -> None:
        if self.keepalive is not None:
            self.keepalive.cancel()
            await asyncio.gather(self.keepalive, return_exceptions=True)
        if self.runner is not None:
            await self.runner.cleanup()
            ten_env.log_info(f"stopped listening on {self.config.listen_port}")

    async def _emit(self, payload: dict) -> Optional[str]:
        """meeting_uploaded to the controller; why it failed, or None."""
        cmd = Cmd.create("meeting_uploaded")
        cmd.set_property_from_json(
            None, json.dumps(payload, ensure_ascii=False)
        )
        try:
            result, err = await self.ten_env.send_cmd(cmd)
        except Exception as failure:  # pylint: disable=broad-except
            return repr(failure)
        if err is not None:
            return err.error_message()
        if result is None or result.get_status_code() != StatusCode.OK:
            return "the controller answered with an error"
        return None
