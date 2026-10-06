#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""An uploaded meeting in, its topics out: segment_audio -> segments_ready."""

import asyncio
import json
from typing import Optional, Set

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
        self.tasks: Set[asyncio.Task] = set()

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
        # Acknowledged now, answered as segments_ready: the runtime fails a
        # command whose result takes over 180 s ("in paths timeout"), and an
        # hour of audio on the board can take that long.
        await ten_env.return_result(CmdResult.create(StatusCode.OK, cmd))
        task = asyncio.create_task(self._segment(ten_env, ogg_path, work_dir))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _segment(
        self, ten_env: AsyncTenEnv, ogg_path: str, work_dir: str
    ) -> None:
        config = self.config or MeetingSegmenterConfig()
        try:
            # Decoding an hour and running the VAD over it takes tens of
            # seconds; on a worker thread, so the loop keeps answering.
            payload = await asyncio.get_running_loop().run_in_executor(
                None,
                segment_file,
                ogg_path,
                work_dir,
                config,
                lambda: load_vad(config),
            )
        except Exception as failure:  # pylint: disable=broad-except
            # Whatever happens, segments_ready goes out: the controller is
            # waiting for it and for nothing else.
            payload = {
                "pcm_path": "",
                "duration_s": 0.0,
                "segments": [],
                "error": f"{type(failure).__name__}: {failure}",
            }
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
