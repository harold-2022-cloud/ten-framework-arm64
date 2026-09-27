#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Audio frames in, segment files out.

The recorder makes no judgements. main_control decides where a topic ends
and says so; this closes the file and reports what it wrote.
"""

import json
import os
import shutil
import time
from typing import Optional

from ten_runtime import (
    AsyncExtension,
    AsyncTenEnv,
    AudioFrame,
    Cmd,
    CmdResult,
    Data,
    StatusCode,
)

from .config import MeetingRecorderConfig
from .recorder import SegmentWriter

CMD_CLOSE_SEGMENT = "close_segment"
DATA_SEGMENT_CLOSED = "segment_closed"


class MeetingRecorderExtension(AsyncExtension):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: Optional[MeetingRecorderConfig] = None
        self.writer: Optional[SegmentWriter] = None

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        config_json, _ = await ten_env.get_property_to_json("")
        self.config = MeetingRecorderConfig.model_validate_json(config_json)

        # Stat the directory we will write to, after making it. Parsing a
        # parent out of the path raises when the parent is the thing that is
        # missing -- which is the one case this check exists for.
        os.makedirs(self.config.output_dir, exist_ok=True)
        free_mb = shutil.disk_usage(self.config.output_dir).free // (
            1024 * 1024
        )
        if free_mb < self.config.min_free_mb:
            ten_env.log_error(
                f"only {free_mb} MB free under {self.config.output_dir}; "
                f"a meeting needs {self.config.min_free_mb} MB. Recording off."
            )
            return

        self.writer = SegmentWriter(self.config.output_dir)
        ten_env.log_info(
            f"meeting_recorder: segments to {self.config.output_dir}, "
            f"{free_mb} MB free"
        )

    async def on_audio_frame(
        self, _ten_env: AsyncTenEnv, audio_frame: AudioFrame
    ) -> None:
        if self.writer is None:
            return
        if not self.writer.is_open:
            self.writer.open(started_at=time.time())
        self.writer.write(bytes(audio_frame.get_buf()))

    async def on_cmd(self, ten_env: AsyncTenEnv, cmd: Cmd) -> None:
        if cmd.get_name() == CMD_CLOSE_SEGMENT and self.writer is not None:
            segment = self.writer.close()
            if segment is not None:
                data = Data.create(DATA_SEGMENT_CLOSED)
                data.set_property_from_json(
                    None,
                    json.dumps(
                        {
                            "path": segment.path,
                            "started_at": segment.started_at,
                            "duration_s": segment.duration_s,
                            "bytes": segment.bytes_written,
                        }
                    ),
                )
                await ten_env.send_data(data)
                ten_env.log_info(
                    f"segment closed: {segment.duration_s:.1f} s, "
                    f"{segment.path}"
                )
        await ten_env.return_result(CmdResult.create(StatusCode.OK, cmd))

    async def on_deinit(self, _ten_env: AsyncTenEnv) -> None:
        if self.writer is not None and self.writer.is_open:
            self.writer.close()
