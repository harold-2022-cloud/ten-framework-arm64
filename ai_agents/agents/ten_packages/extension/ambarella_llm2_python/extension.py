#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
from typing import AsyncGenerator, Optional

from ten_ai_base.llm2 import AsyncLLM2BaseExtension
from ten_ai_base.struct import (
    LLMRequest,
    LLMRequestRetrievePrompt,
    LLMResponse,
    LLMResponseRetrievePrompt,
)
from .ambarella import AmbarellaChatClient, AmbarellaLLM2Config
from ten_runtime import (
    AsyncTenEnv,
)


class AmbarellaLLM2Extension(AsyncLLM2BaseExtension):
    """
    Drop-in provider for the LLM demo server running on an Ambarella AI
    Developer Kit, mirroring DifyLLM2Extension's structure:
    - loads config on start
    - forwards on_call_chat_completion to client.get_chat_completions
    """

    def __init__(self, name: str):
        super().__init__(name)
        self.config: Optional[AmbarellaLLM2Config] = None
        self.client: Optional[AmbarellaChatClient] = None

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        ten_env.log_info("on_init")
        await super().on_init(ten_env)

    async def on_start(self, async_ten_env: AsyncTenEnv) -> None:
        async_ten_env.log_info("on_start")
        await super().on_start(async_ten_env)

        # Load config
        config_json, _ = await self.ten_env.get_property_to_json("")
        self.config = AmbarellaLLM2Config.model_validate_json(config_json)

        # The board's interface is unauthenticated, so base_url is the only
        # mandatory property.
        if not self.config.base_url:
            async_ten_env.log_error("base_url is missing, exiting on_start")
            return

        if self.config.response_format not in ("auto", "sse", "raw"):
            async_ten_env.log_error(
                f"invalid response_format {self.config.response_format!r}, "
                "expected auto, sse or raw; exiting on_start"
            )
            return

        # Create client
        try:
            self.client = AmbarellaChatClient(async_ten_env, self.config)
            async_ten_env.log_info(
                "initialized Ambarella client: "
                f"base_url={self.config.base_url}, "
                f"model_type={self.config.model_type}, "
                f"response_format={self.config.response_format}"
            )
        except Exception as err:
            async_ten_env.log_error(
                f"Failed to initialize AmbarellaChatClient: {err}"
            )

    async def on_stop(self, async_ten_env: AsyncTenEnv) -> None:
        async_ten_env.log_info("on_stop")
        if self.client:
            await self.client.aclose()
        await super().on_stop(async_ten_env)

    async def on_deinit(self, async_ten_env: AsyncTenEnv) -> None:
        async_ten_env.log_info("on_deinit")
        await super().on_deinit(async_ten_env)

    async def on_retrieve_prompt(
        self, async_ten_env: AsyncTenEnv, request: LLMRequestRetrievePrompt
    ) -> LLMResponseRetrievePrompt:
        """
        The board has no system role; the prompt is folded into the first
        query instead, so report back whatever was configured.
        """
        prompt = self.config.prompt if self.config else ""
        async_ten_env.log_info(
            f"Retrieved prompt for request_id: {request.request_id}"
        )
        return LLMResponseRetrievePrompt(prompt=prompt)

    def on_call_chat_completion(
        self, async_ten_env: AsyncTenEnv, request_input: LLMRequest
    ) -> AsyncGenerator[LLMResponse, None]:
        # Delegate to provider client (matches DifyLLM2Extension)
        if self.client is None:
            # on_start returns early on a missing base_url or an invalid
            # response_format, leaving no client behind. Without this the
            # first turn dies on an AttributeError that names neither cause.
            raise RuntimeError(
                "Ambarella client is not initialized; check on_start logs "
                "for a missing base_url or an invalid response_format"
            )
        async_ten_env.log_debug(
            "on_call_chat_completion: "
            f"{len(request_input.messages or [])} message(s)"
        )
        return self.client.get_chat_completions(request_input)
