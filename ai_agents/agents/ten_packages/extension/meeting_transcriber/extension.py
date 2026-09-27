#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A file in, a transcript out, one segment at a time."""

import json
from typing import Optional

from ten_runtime import (
    AsyncExtension,
    AsyncTenEnv,
    Cmd,
    CmdResult,
    Data,
    StatusCode,
)

from .config import MeetingTranscriberConfig
from .transcriber import MeetingTranscriber

CMD_TRANSCRIBE = "transcribe"
DATA_SEGMENT_TRANSCRIBED = "segment_transcribed"


class MeetingTranscriberExtension(AsyncExtension):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: Optional[MeetingTranscriberConfig] = None
        self.transcriber: Optional[MeetingTranscriber] = None

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        config_json, _ = await ten_env.get_property_to_json("")
        self.config = MeetingTranscriberConfig.model_validate_json(config_json)
        try:
            self.config.validate_models()
        except ValueError as err:
            ten_env.log_error(f"meeting_transcriber unusable: {err}")
            return
        self.transcriber = MeetingTranscriber(self.config, ten_env)
        ten_env.log_info(
            f"meeting_transcriber ready: {self.config.num_threads} threads"
        )

    async def on_cmd(self, ten_env: AsyncTenEnv, cmd: Cmd) -> None:
        if cmd.get_name() != CMD_TRANSCRIBE:
            await ten_env.return_result(CmdResult.create(StatusCode.OK, cmd))
            return

        path, _ = cmd.get_property_string("path")
        segment_id, _ = cmd.get_property_string("segment_id")
        speakers, err = cmd.get_property_int("speakers")
        if err:
            speakers = self.config.speakers if self.config else -1
        if speakers == -1:
            # Not fatal -- auto-detection is a legitimate choice -- but on
            # the board an unspecified count clustered four speakers into
            # seven. Worth a line in the log before the transcript arrives
            # over-split.
            ten_env.log_warn(
                "meeting_transcriber: speakers=-1, clustering will guess "
                "the speaker count; expect over-splitting (one speaker "
                "coming back as several)"
            )

        payload = {"segment_id": segment_id, "utterances": [], "error": None}
        if self.transcriber is None:
            payload["error"] = "transcriber was never initialised"
        else:
            try:
                utterances = await self.transcriber.transcribe(path, speakers)
                payload["utterances"] = [
                    {
                        "start_s": u.start_s,
                        "end_s": u.end_s,
                        "speaker": u.speaker,
                        "text": u.text,
                    }
                    for u in utterances
                ]
            except Exception as failure:  # pylint: disable=broad-except
                # One segment failing must not take the meeting with it: the
                # record says which one, and the rest stands.
                payload["error"] = str(failure)
                ten_env.log_error(f"segment {segment_id} failed: {failure}")

        data = Data.create(DATA_SEGMENT_TRANSCRIBED)
        data.set_property_from_json(
            None, json.dumps(payload, ensure_ascii=False)
        )
        await ten_env.send_data(data)
        await ten_env.return_result(CmdResult.create(StatusCode.OK, cmd))
