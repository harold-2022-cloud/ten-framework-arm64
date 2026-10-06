#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""One question to the board's LLM, one answer back, nothing remembered.

Not the conversation agent's LLMExec. That keeps every earlier question and
answer and sends them all with each new one -- right for a conversation,
wrong here: the tenth topic's summary would carry the first nine topics'
transcripts and summaries, and on the board's 7B every addition to a prompt
cost content (PRD Part 2). Here each question goes alone.

A turn past timeout_s is aborted on the llm extension, so its answer cannot
arrive later and be taken for the next question's: answers carry nothing
that says which question they belong to.
"""

import asyncio
import json
import uuid

from ten_ai_base.struct import (
    LLMMessageContent,
    LLMRequest,
    LLMResponseMessageDelta,
    LLMResponseMessageDone,
    parse_llm_response,
)
from ten_runtime import Cmd, Loc, StatusCode


class LLMClient:
    def __init__(self, ten_env, dest: str = "llm", timeout_s: float = 180.0):
        self.ten_env = ten_env
        self.dest = dest
        self.timeout_s = timeout_s

    async def ask(self, prompt: str) -> str:
        """The answer, or "" when there is none in time."""
        request_id = str(uuid.uuid4())
        request = LLMRequest(
            request_id=request_id,
            messages=[LLMMessageContent(role="user", content=prompt)],
            streaming=True,
        )
        try:
            return await asyncio.wait_for(
                self._collect(request), timeout=self.timeout_s
            )
        except asyncio.TimeoutError:
            self.ten_env.log_error(
                f"LLM turn {request_id} gave no answer in {self.timeout_s} s; "
                "aborting it"
            )
            await self._send("abort", {"request_id": request_id})
            return ""

    async def _collect(self, request: LLMRequest) -> str:
        # No nulls: the runtime checks chat_completion against ten_ai_base's
        # llm-interface.json and refuses "tools": null where it wants an
        # array, before the llm extension ever sees the question.
        cmd = self._cmd(
            "chat_completion", request.model_dump(exclude_none=True)
        )
        text = ""
        async for result, _ in self.ten_env.send_cmd_ex(cmd):
            if result is None:
                continue
            payload, _ = result.get_property_to_json(None)
            if result.get_status_code() != StatusCode.OK:
                self.ten_env.log_error(
                    f"LLM turn {request.request_id} failed: "
                    f"{result.get_status_code()} {payload}"
                )
                return ""
            if payload:
                response = parse_llm_response(payload)
                if isinstance(response, LLMResponseMessageDone):
                    return response.content or text
                if isinstance(response, LLMResponseMessageDelta):
                    text = response.content or text
            if result.is_final():
                break
        return text

    def _cmd(self, name: str, payload: dict) -> Cmd:
        cmd = Cmd.create(name)
        cmd.set_dests([Loc("", "", self.dest)])
        cmd.set_property_from_json(
            None, json.dumps(payload, ensure_ascii=False)
        )
        return cmd

    async def _send(self, name: str, payload: dict) -> None:
        try:
            await self.ten_env.send_cmd(self._cmd(name, payload))
        except Exception as err:  # pylint: disable=broad-except
            self.ten_env.log_error(f"could not send {name}: {err}")
