#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

from pydantic import BaseModel


class MeetingControlConfig(BaseModel):
    """What to ask the board's LLM, how long to wait, how to link voices."""

    # One LLM turn -- a topic's summary or the meeting's conclusion -- may
    # stay outstanding this long before it is given up on. Bounded so a hung
    # board costs one summary rather than every topic after it.
    summary_timeout_s: float = 180.0

    # Short on purpose: on the board's 7B every addition to a prompt cost
    # content, even corrections that were each right (PRD Part 2).
    segment_prompt: str = (
        "以下是一段會議的逐字稿，每行標了時間和說話人。"
        "用中文條列這一段的重點，以及被提出的待辦事項，標上時間和說話人。"
        "不要客套話。\n\n"
    )
    meeting_prompt: str = (
        "以下是一場會議各段的重點。用中文整理出：一、結論；"
        "二、待辦事項，標上提出的時間和人。不要客套話。\n\n"
    )

    # Used when an upload does not say how many people were there.
    speakers: int = -1

    # Linking voices across topics (speakers.py, PRD Part 2).
    link_over_factor: int = 3
    link_threshold: float = 0.4
    link_same_voice: float = 0.55
