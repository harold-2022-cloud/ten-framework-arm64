#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
from typing import Type

from .events import LLMResponseEvent


def agent_event_handler(event_type: Type[LLMResponseEvent]):
    """
    Decorator to mark a method as an Agent event handler.
    Usage:
        @agent_event_handler(LLMResponseEvent)
        async def on_llm(self, event: LLMResponseEvent): ...
    """

    def wrapper(func):
        setattr(func, "_agent_event_type", event_type)
        return func

    return wrapper
