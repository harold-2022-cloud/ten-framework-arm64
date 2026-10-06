#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""An uploaded meeting in, its topics out: segment_audio -> segments_ready."""

import asyncio
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

from .config import MeetingSegmenterConfig
from .segmenter import load_vad, segment_file

CMD_SEGMENT_AUDIO = "segment_audio"
DATA_SEGMENTS_READY = "segments_ready"


class MeetingSegmenterExtension(AsyncExtension):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: Optional[MeetingSegmenterConfig] = None

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        config_json, _ = await ten_env.get_property_to_json("")
        self.config = MeetingSegmenterConfig.model_validate_json(config_json)
        ten_env.log_info(
            f"meeting_segmenter ready: silence {self.config.segment_silence_s} s, "
            f"topics {self.config.min_segment_s}-{self.config.max_segment_s} s, "
            f"vad {self.config.vad_model!r} at {self.config.vad_threshold}"
        )

    async def on_cmd(self, ten_env: AsyncTenEnv, cmd: Cmd) -> None:
        if cmd.get_name() != CMD_SEGMENT_AUDIO:
            await ten_env.return_result(CmdResult.create(StatusCode.OK, cmd))
            return

        ogg_path, _ = cmd.get_property_string("ogg_path")
        work_dir, _ = cmd.get_property_string("work_dir")
        config = self.config or MeetingSegmenterConfig()
        # Decoding an hour and running the VAD over it takes tens of seconds;
        # on a worker thread, so the loop keeps answering.
        payload = await asyncio.get_running_loop().run_in_executor(
            None,
            segment_file,
            ogg_path,
            work_dir,
            config,
            lambda: load_vad(config),
        )
        if payload["error"]:
            ten_env.log_error(
                f"segmenting {ogg_path} failed: {payload['error']}"
            )
        else:
            ten_env.log_info(
                f"segmented {ogg_path}: {payload['duration_s']:.1f} s, "
                f"{len(payload['segments'])} topics"
            )

        data = Data.create(DATA_SEGMENTS_READY)
        data.set_property_from_json(None, json.dumps(payload))
        await ten_env.send_data(data)
        await ten_env.return_result(CmdResult.create(StatusCode.OK, cmd))
