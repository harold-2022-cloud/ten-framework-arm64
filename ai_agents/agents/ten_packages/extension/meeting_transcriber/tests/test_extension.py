#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The extension through the TEN runtime, the way a graph loads it.

The slice test needs the three models; point DIARIZATION_SEG_MODEL,
DIARIZATION_EMB_MODEL and SENSEVOICE_MODEL_DIR at them, the same names the
graph reads, or it skips rather than passes.
"""

import asyncio
import json
import os

import numpy as np
import pytest
from ten_runtime import (
    AsyncExtensionTester,
    AsyncTenEnvTester,
    Cmd,
    Data,
    StatusCode,
)

ADDON = "meeting_transcriber"
SEG = os.environ.get("DIARIZATION_SEG_MODEL", "")
EMB = os.environ.get("DIARIZATION_EMB_MODEL", "")
ASR = os.environ.get("SENSEVOICE_MODEL_DIR", "")


class TranscribeTester(AsyncExtensionTester):
    """Sends one transcribe and keeps what comes back."""

    def __init__(self, props: dict) -> None:
        super().__init__()
        self.props = props
        self.payload = None
        self.status = None
        self.timed_out = False

    async def on_start(self, ten_env: AsyncTenEnvTester) -> None:
        asyncio.create_task(self._watchdog(ten_env))
        cmd = Cmd.create("transcribe")
        cmd.set_property_from_json(None, json.dumps(self.props))
        result, _ = await ten_env.send_cmd(cmd)
        self.status = result.get_status_code() if result else None
        if self.payload is not None:
            ten_env.stop_test()

    async def on_data(self, ten_env: AsyncTenEnvTester, data: Data) -> None:
        if data.get_name() == "segment_transcribed":
            payload, _ = data.get_property_to_json(None)
            self.payload = json.loads(payload)
            if self.status is not None:
                ten_env.stop_test()

    async def _watchdog(self, ten_env: AsyncTenEnvTester) -> None:
        await asyncio.sleep(120)
        self.timed_out = True
        ten_env.stop_test()


def run(cmd_props: dict, **ext_props) -> TranscribeTester:
    tester = TranscribeTester(cmd_props)
    tester.set_test_mode_single(ADDON, json.dumps(ext_props))
    err = tester.run()
    assert err is None, err.error_message()
    assert not tester.timed_out, "no segment_transcribed within 120 s"
    return tester


def test_without_models_the_answer_is_an_error_naming_the_topic(tmp_path):
    tester = run(
        {
            "pcm_path": str(tmp_path / "a.pcm"),
            "segment_id": "t03",
            "speakers": 2,
        },
        segmentation_model="/missing.onnx",
        embedding_model="/missing.onnx",
        asr_model_dir="/missing",
    )

    assert tester.status == StatusCode.OK
    assert tester.payload["segment_id"] == "t03"
    assert tester.payload["utterances"] == []
    assert tester.payload["error"]


@pytest.mark.skipif(
    not (os.path.isfile(SEG) and os.path.isfile(EMB) and os.path.isdir(ASR)),
    reason="model paths not set",
)
def test_a_slice_of_the_meeting_pcm_is_read_by_pcm_path(tmp_path):
    pcm = tmp_path / "audio.pcm"
    np.zeros(16000 * 3, dtype=np.int16).tofile(pcm)

    tester = run(
        {
            "pcm_path": str(pcm),
            "segment_id": "t01",
            "speakers": 2,
            "start_s": 1.0,
            "duration_s": 1.0,
        },
        segmentation_model=SEG,
        embedding_model=EMB,
        asr_model_dir=ASR,
    )

    assert tester.status == StatusCode.OK
    assert tester.payload["error"] is None
    assert tester.payload["utterances"] == []  # silence holds no speech
