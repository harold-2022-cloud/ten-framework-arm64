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
    # content, even corrections that were each right (PRD Part 2). Simplified
    # characters, and no asking for times: the earlier "條列重點……標上時間和
    # 說話人" had the model copy a topic's transcript line by line, twice, in
    # 346 s; this one summarised it in 11 s (probe_meeting_summary.py).
    #
    # People by speaker number, in both: asked "who does what" from
    # summaries that named nobody, the conclusion gave six action items to
    # 刘、张、李、王、赵、陈老师 -- none of them in the meeting. With the
    # summaries saying 说话人3 and the conclusion told to use those numbers or
    # leave the owner out, it invented no one (--conclusions, 2026-10-07).
    # The numbers are the record's own, so the app's renaming applies.
    segment_prompt: str = (
        "以下是一段会议的逐字稿。用中文写三到五句话，总结这段在讨论什么、"
        "做了什么决定、谁要做什么。提到人时用逐字稿里的说话人编号。"
        "不要逐句复述原文。\n\n"
    )
    meeting_prompt: str = (
        "以下是一场会议各段的总结。用中文写出：一、这场会议的结论；"
        "二、待办事项，负责人用总结里的说话人编号，没有提到的就不写。"
        "不要逐句复述原文。\n\n"
    )

    # Used when an upload does not say how many people were there.
    speakers: int = -1

    # Linking voices across topics (speakers.py, PRD Part 2).
    link_over_factor: int = 3
    link_threshold: float = 0.4
    link_same_voice: float = 0.55
