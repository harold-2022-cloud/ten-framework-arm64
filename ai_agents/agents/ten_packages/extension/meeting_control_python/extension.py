#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The meeting graph's controller: MeetingFlow wired to the TEN runtime.

Commands to the segmenter and the transcriber are sent without waiting on
their results -- the answers come back as data -- but a result that says
the command itself failed is turned into an error for the flow, so a
missing or broken extension ends the meeting failed instead of leaving it
in "decoding" forever.

Every callback catches what it raises: the runtime calls os._exit(1) on an
uncaught exception in an extension callback, which would take the whole
worker, and any other meeting's state, with it.
"""

import asyncio
import json
from typing import Optional, Set

from ten_runtime import (
    AsyncExtension,
    AsyncTenEnv,
    Cmd,
    CmdResult,
    Data,
    StatusCode,
)

from .config import MeetingControlConfig
from .flow import MeetingFlow
from .llm import LLMClient


class _Ports:
    """What MeetingFlow needs from the graph."""

    def __init__(self, ten_env: AsyncTenEnv, llm: LLMClient, flow_ref) -> None:
        self.ten_env = ten_env
        self.llm = llm
        self.flow_ref = flow_ref
        self.tasks: Set[asyncio.Task] = set()

    async def segment_audio(self, ogg_path: str, work_dir: str) -> None:
        cmd = Cmd.create("segment_audio")
        cmd.set_property_string("ogg_path", ogg_path)
        cmd.set_property_string("work_dir", work_dir)

        async def failed(reason: str) -> None:
            await self.flow_ref().on_segments_ready(
                {"segments": [], "error": f"segment_audio failed: {reason}"}
            )

        self._send(cmd, failed)

    async def transcribe(
        self, pcm_path: str, segment_id: str, start_s, duration_s, speakers
    ) -> None:
        cmd = Cmd.create("transcribe")
        cmd.set_property_string("pcm_path", pcm_path)
        cmd.set_property_string("segment_id", segment_id)
        cmd.set_property_float("start_s", float(start_s))
        cmd.set_property_float("duration_s", float(duration_s))
        cmd.set_property_int("speakers", int(speakers))

        async def failed(reason: str) -> None:
            await self.flow_ref().on_transcribed(
                {
                    "segment_id": segment_id,
                    "utterances": [],
                    "error": f"transcribe failed: {reason}",
                }
            )

        self._send(cmd, failed)

    async def ask_llm(self, prompt: str) -> str:
        return await self.llm.ask(prompt)

    def log(self, message: str) -> None:
        self.ten_env.log_info(message)

    def _send(self, cmd: Cmd, on_failure) -> None:
        # Read before sending: once send_cmd takes the command the runtime
        # invalidates it, and cmd.get_name() raises "Msg is invalidated".
        name = cmd.get_name()

        async def send() -> None:
            try:
                result, err = await self.ten_env.send_cmd(cmd)
                if err is not None or (
                    result is not None
                    and result.get_status_code() != StatusCode.OK
                ):
                    reason = err.error_message() if err else "status ERROR"
                    self.ten_env.log_error(f"{name} failed: {reason}")
                    await on_failure(reason)
            except Exception as failure:  # pylint: disable=broad-except
                self.ten_env.log_error(f"{name} failed: {failure}")

        task = asyncio.create_task(send())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


class MeetingControlExtension(AsyncExtension):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.ten_env: Optional[AsyncTenEnv] = None
        self.flow: Optional[MeetingFlow] = None
        self.tasks: Set[asyncio.Task] = set()

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        self.ten_env = ten_env
        config_json, _ = await ten_env.get_property_to_json("")
        config = MeetingControlConfig.model_validate_json(config_json)
        llm = LLMClient(ten_env, dest="llm", timeout_s=config.summary_timeout_s)
        self.flow = MeetingFlow(config, _Ports(ten_env, llm, lambda: self.flow))
        ten_env.log_info("meeting_control_python ready")

    async def on_cmd(self, ten_env: AsyncTenEnv, cmd: Cmd) -> None:
        if cmd.get_name() == "meeting_uploaded":
            payload_json, _ = cmd.get_property_to_json(None)
            self._run(self.flow.start, payload_json)
        await ten_env.return_result(CmdResult.create(StatusCode.OK, cmd))

    async def on_data(self, _ten_env: AsyncTenEnv, data: Data) -> None:
        name = data.get_name()
        payload_json, _ = data.get_property_to_json(None)
        if name == "segments_ready":
            self._run(self.flow.on_segments_ready, payload_json)
        elif name == "segment_transcribed":
            self._run(self.flow.on_transcribed, payload_json)

    def _run(self, handler, payload_json: str) -> None:
        """Off the callback, so an hour of summaries never holds the loop,
        and guarded, so nothing raised reaches the runtime."""

        async def guarded() -> None:
            try:
                await handler(json.loads(payload_json))
            except Exception as failure:  # pylint: disable=broad-except
                self.ten_env.log_error(
                    f"meeting flow failed in {handler.__name__}: {failure}"
                )

        task = asyncio.create_task(guarded())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
