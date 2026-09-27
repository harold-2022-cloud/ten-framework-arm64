#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

from pydantic import BaseModel


class MeetingControlConfig(BaseModel):
    """When a topic ends, when the meeting does, and what to ask for."""

    # A meeting is not people talking without pause. Somebody reads a
    # document, somebody waits for a latecomer, a topic finishes. Thirty
    # seconds is a starting point and has not been checked against a real
    # meeting's recording.
    segment_silence_s: float = 30.0
    # Generous on purpose: getting this wrong only means the record is
    # assembled a little late, and the segments are already done.
    meeting_silence_s: float = 600.0
    # Below this, a segment is not worth a diarization pass and an LLM turn.
    # It joins the next one.
    min_segment_s: float = 5.0

    speakers: int = -1

    segment_prompt: str = (
        "以下是一段會議錄音的逐字稿。用中文條列這一段的重點，"
        "以及任何被提出的待辦事項，每一項標上它的時間。不要客套話。\n\n"
    )
    meeting_prompt: str = (
        "以下是一場會議各段的重點。用中文整理出：一、結論；"
        "二、待辦事項，每一項標上提出的時間和提出的人。不要客套話。\n\n"
    )
