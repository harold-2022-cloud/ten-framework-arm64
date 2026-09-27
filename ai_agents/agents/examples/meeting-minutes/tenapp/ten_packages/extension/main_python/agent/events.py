#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The one agent event this pipeline drives: the board's LLM turn coming
back. There is no ASR, no RTC user join/leave, and no tool calling on this
graph -- meeting-minutes only ever asks the board one thing, one topic at a
time, and waits for the answer.
"""

from typing import Literal

from pydantic import BaseModel


class LLMResponseEvent(BaseModel):
    """The board answered one LLM turn, streamed or final.

    ``type`` distinguishes a reasoning token from the message itself: the
    board can start a turn in reasoning mode (``starts_in_reasoning``), and
    only a final *message* is a summary worth keeping.
    """

    type: Literal["message", "reasoning"] = "message"
    delta: str
    text: str
    is_final: bool
