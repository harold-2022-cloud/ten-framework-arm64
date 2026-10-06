#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The extension through the TEN runtime, the way a graph loads it.

Covers what the segmenter tests cannot reach: the addon registers under its
manifest name, properties are injected, segment_audio arrives with its two
paths, and segments_ready leaves carrying the payload. Run with

    task test-extension EXTENSION=agents/ten_packages/extension/meeting_segmenter

and MEETING_VAD_MODEL pointing at ten-vad.onnx for the test that needs it.
"""

import asyncio
import json
import os

import numpy as np
import pytest
import soundfile as sf
from ten_runtime import (
    AsyncExtensionTester,
    AsyncTenEnvTester,
    Cmd,
    Data,
    StatusCode,
)

ADDON = "meeting_segmenter"
MODEL = os.environ.get("MEETING_VAD_MODEL", "")


class SegmentTester(AsyncExtensionTester):
    """Sends one segment_audio and keeps what comes back."""

    def __init__(self, ogg_path: str, work_dir: str) -> None:
        super().__init__()
        self.ogg_path = ogg_path
        self.work_dir = work_dir
        self.payload = None
        self.status = None
        self.timed_out = False
        self.order = []

    async def on_start(self, ten_env: AsyncTenEnvTester) -> None:
        asyncio.create_task(self._watchdog(ten_env))
        cmd = Cmd.create("segment_audio")
        cmd.set_property_string("ogg_path", self.ogg_path)
        cmd.set_property_string("work_dir", self.work_dir)
        result, _ = await ten_env.send_cmd(cmd)
        self.order.append("result")
        self.status = result.get_status_code() if result else None
        await asyncio.sleep(0)
        if self.payload is not None:
            ten_env.stop_test()

    async def on_data(self, ten_env: AsyncTenEnvTester, data: Data) -> None:
        if data.get_name() == "segments_ready":
            payload, _ = data.get_property_to_json(None)
            self.payload = json.loads(payload)
            self.order.append("data")
            if self.status is not None:
                ten_env.stop_test()

    async def _watchdog(self, ten_env: AsyncTenEnvTester) -> None:
        await asyncio.sleep(60)
        self.timed_out = True
        ten_env.stop_test()


def run(tmp_path, ogg_path, **props):
    tester = SegmentTester(str(ogg_path), str(tmp_path))
    tester.set_test_mode_single(ADDON, json.dumps(props))
    err = tester.run()
    assert err is None, err.error_message()
    assert not tester.timed_out, "no segments_ready within 60 s"
    return tester


def test_an_undecodable_upload_comes_back_as_an_error_not_a_crash(tmp_path):
    ogg = tmp_path / "audio.ogg"
    ogg.write_bytes(b"not an ogg")

    tester = run(tmp_path, ogg, vad_model="/unused.onnx")

    assert tester.status == StatusCode.OK
    assert tester.payload["segments"] == []
    assert tester.payload["error"]


def test_the_command_is_answered_before_the_audio_is_decoded(tmp_path):
    """Decoding and scanning an hour of audio may outlast the runtime's
    180 s command timeout on the board; the transcriber's topics did. The
    command is only an acknowledgement; the topics come as data."""
    ogg = tmp_path / "audio.ogg"
    ogg.write_bytes(b"not an ogg")

    tester = run(tmp_path, ogg, vad_model="/unused.onnx")

    assert tester.order == ["result", "data"]


@pytest.mark.skipif(
    not os.path.isfile(MODEL), reason="MEETING_VAD_MODEL not set"
)
def test_a_real_recording_comes_back_with_its_length(tmp_path):
    ogg = tmp_path / "audio.ogg"
    t = np.arange(3 * 16000) / 16000
    sf.write(
        str(ogg),
        0.3 * np.sin(2 * np.pi * 440 * t),
        16000,
        format="OGG",
        subtype="OPUS",
    )

    tester = run(tmp_path, ogg, vad_model=MODEL)

    assert tester.status == StatusCode.OK
    assert tester.payload["error"] is None
    assert tester.payload["duration_s"] == 3.0
    assert tester.payload["pcm_path"] == str(tmp_path / "audio.pcm")
