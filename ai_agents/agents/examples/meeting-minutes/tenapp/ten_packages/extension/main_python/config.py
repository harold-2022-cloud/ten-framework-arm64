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

    # Frames stop arriving when the upload goes away -- and nothing else
    # says so: websocket_server only logs a client disconnect, so a drop
    # mid-sentence produces no end_of_sentence ever. A gap this long in a
    # stream the client sends continuously (it streams, it does not gate on
    # voice) is a dead socket, not a pause: three seconds is tens to
    # hundreds of missed frames, far past any scheduling hiccup or TCP
    # retransmit on a LAN, while staying an order of magnitude under
    # segment_silence_s so that declaring the upload gone can never
    # pre-empt a real topic boundary. The whole cost of being wrong is
    # three seconds on the clock that produces the record.
    upload_gone_s: float = 3.0

    # How long one LLM turn -- a segment's summary or the whole meeting's --
    # may stay outstanding before it is given up on. Bounded so a hung board
    # loses one summary rather than stalling every topic after it; the
    # board itself holds a session for up to 180 s after a reply.
    summary_timeout_s: float = 180.0

    segment_prompt: str = (
        "以下是一段會議錄音的逐字稿。用中文條列這一段的重點，"
        "以及任何被提出的待辦事項，每一項標上它的時間。不要客套話。\n\n"
    )
    meeting_prompt: str = (
        "以下是一場會議各段的重點。用中文整理出：一、結論；"
        "二、待辦事項，每一項標上提出的時間和提出的人。不要客套話。\n\n"
    )
