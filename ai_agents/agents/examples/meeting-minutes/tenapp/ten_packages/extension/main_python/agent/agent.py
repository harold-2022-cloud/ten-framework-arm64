#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Wraps LLMExec in the same event-dispatch shape voice-assistant's Agent
uses, trimmed to what a meeting has: one topic asked about at a time, one
answer back. There is no ASR queue, no RTC user join/leave, and no tool
registry here -- nothing upstream of main_control emits those, and this
graph never registers a tool.
"""

import asyncio
from typing import Awaitable, Callable, Optional, Type

from ten_runtime import AsyncTenEnv

from .events import LLMResponseEvent
from .llm_exec import LLMExec


class Agent:
    def __init__(self, ten_env: AsyncTenEnv) -> None:
        self.ten_env: AsyncTenEnv = ten_env
        self.stopped = False

        # Callback registry
        self._callbacks: dict[
            Type[LLMResponseEvent],
            list[Callable[[LLMResponseEvent], Awaitable]],
        ] = {}

        # Queue for ordered processing: one turn finishes before the next
        # is dispatched, matching the board's own one-turn-at-a-time limit.
        self._llm_queue: asyncio.Queue[LLMResponseEvent] = asyncio.Queue()
        self._llm_active_task: Optional[asyncio.Task] = None

        self.llm_exec = LLMExec(ten_env)
        self.llm_exec.on_response = self._on_llm_response
        self.llm_exec.on_reasoning_response = self._on_llm_reasoning_response

        self._llm_consumer: asyncio.Task = asyncio.create_task(
            self._consume_llm()
        )

    # === Register handlers ===
    def on(
        self,
        event_type: Type[LLMResponseEvent],
        handler: Callable[[LLMResponseEvent], Awaitable],
    ) -> None:
        """Register a callback for a given event type."""
        self._callbacks.setdefault(event_type, []).append(handler)

    async def _dispatch(self, event: LLMResponseEvent) -> None:
        """Dispatch event to registered handlers sequentially."""
        for etype, handlers in self._callbacks.items():
            if isinstance(event, etype):
                for h in handlers:
                    # asyncio.CancelledError is a BaseException, not an
                    # Exception, since Python 3.8 -- it propagates through
                    # this on its own.
                    try:
                        await h(event)
                    except Exception as exc:
                        self.ten_env.log_error(
                            f"Handler error for {etype}: {exc}"
                        )

    # === Consumer ===
    async def _consume_llm(self) -> None:
        while not self.stopped:
            event = await self._llm_queue.get()
            # Run handler as a task so we can cancel mid-flight
            self._llm_active_task = asyncio.create_task(self._dispatch(event))
            try:
                await self._llm_active_task
            except asyncio.CancelledError:
                self.ten_env.log_info("[Agent] Active LLM task cancelled")
            finally:
                self._llm_active_task = None

    async def _emit_llm(self, event: LLMResponseEvent) -> None:
        await self._llm_queue.put(event)

    # === Incoming from LLMExec ===
    async def _on_llm_response(
        self, _ten_env: AsyncTenEnv, delta: str, text: str, is_final: bool
    ) -> None:
        await self._emit_llm(
            LLMResponseEvent(delta=delta, text=text, is_final=is_final)
        )

    async def _on_llm_reasoning_response(
        self, _ten_env: AsyncTenEnv, delta: str, text: str, is_final: bool
    ) -> None:
        await self._emit_llm(
            LLMResponseEvent(
                delta=delta, text=text, is_final=is_final, type="reasoning"
            )
        )

    # === LLM control ===
    async def queue_llm_input(self, text: str) -> None:
        """Queue a new message to the LLM context."""
        await self.llm_exec.queue_input(text)

    async def flush_llm(self) -> None:
        """Flush the LLM input queue and cancel whatever is in flight."""
        await self.llm_exec.flush()

        while not self._llm_queue.empty():
            try:
                self._llm_queue.get_nowait()
                self._llm_queue.task_done()
            except asyncio.QueueEmpty:
                break

        if self._llm_active_task and not self._llm_active_task.done():
            self._llm_active_task.cancel()
            try:
                await self._llm_active_task
            except asyncio.CancelledError:
                pass
            self._llm_active_task = None

    async def stop(self) -> None:
        """Stop the agent processing and any ongoing tasks."""
        self.stopped = True
        await self.llm_exec.stop()
        await self.flush_llm()
        if self._llm_consumer:
            self._llm_consumer.cancel()
