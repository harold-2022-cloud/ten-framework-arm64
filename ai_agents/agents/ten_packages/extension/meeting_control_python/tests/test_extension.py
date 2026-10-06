#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The controller through the TEN runtime, the way the graph loads it.

The tester plays the segmenter and the transcriber: it answers the commands
the controller sends and sends back the data those extensions would. There
is no llm node here, so every summary comes back empty -- which is itself
the point of one test: a meeting still ends archived without its LLM.

    task test-extension \\
      EXTENSION=agents/examples/voice-assistant/tenapp/ten_packages/extension/meeting_control_python
"""

import asyncio
import json
import os

from ten_runtime import (
    AsyncExtensionTester,
    AsyncTenEnvTester,
    Cmd,
    CmdResult,
    Data,
    StatusCode,
)

ADDON = "meeting_control_python"


def state(work_dir):
    path = os.path.join(work_dir, "state.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class GraphTester(AsyncExtensionTester):
    """Uploads one meeting and plays the extensions downstream of it."""

    def __init__(self, folder: str, segmenter_ok: bool = True) -> None:
        super().__init__()
        self.folder = folder
        self.work = os.path.join(folder, "work")
        self.segmenter_ok = segmenter_ok
        self.received = []
        self.final = None

    async def on_start(self, ten_env: AsyncTenEnvTester) -> None:
        cmd = Cmd.create("meeting_uploaded")
        cmd.set_property_from_json(
            None,
            json.dumps(
                {
                    "meeting_id": "m1",
                    "ogg_path": os.path.join(self.folder, "audio.ogg"),
                    "work_dir": self.work,
                    "title": "週會",
                    "speakers": 2,
                }
            ),
        )
        await ten_env.send_cmd(cmd)
        asyncio.create_task(self._wait_for_the_end(ten_env))

    async def on_cmd(self, ten_env: AsyncTenEnvTester, cmd: Cmd) -> None:
        name = cmd.get_name()
        self.received.append(name)
        if name == "segment_audio" and not self.segmenter_ok:
            await ten_env.return_result(CmdResult.create(StatusCode.ERROR, cmd))
            return
        await ten_env.return_result(CmdResult.create(StatusCode.OK, cmd))
        if name == "segment_audio":
            await self._send(
                ten_env,
                "segments_ready",
                {
                    "pcm_path": os.path.join(self.work, "audio.pcm"),
                    "duration_s": 300.0,
                    "segments": [{"id": "t01", "start_s": 0.0, "end_s": 300.0}],
                    "error": None,
                },
            )
        elif name == "transcribe":
            await self._send(
                ten_env,
                "segment_transcribed",
                {
                    "segment_id": "t01",
                    "utterances": [
                        {
                            "start_s": 1.0,
                            "end_s": 5.0,
                            "speaker": 0,
                            "text": "開始吧。",
                            "embedding": [1.0, 0.0],
                        },
                        {
                            "start_s": 6.0,
                            "end_s": 9.0,
                            "speaker": 1,
                            "text": "好。",
                            "embedding": [0.0, 1.0],
                        },
                    ],
                    "error": None,
                },
            )

    async def _send(self, ten_env, name, payload):
        data = Data.create(name)
        data.set_property_from_json(
            None, json.dumps(payload, ensure_ascii=False)
        )
        await ten_env.send_data(data)

    async def _wait_for_the_end(self, ten_env):
        for _ in range(600):
            current = state(self.work).get("state")
            if current in ("archived", "failed", "empty"):
                self.final = state(self.work)
                break
            await asyncio.sleep(0.1)
        ten_env.stop_test()


def run(tmp_path, **kwargs) -> GraphTester:
    folder = tmp_path / "m1"
    (folder / "work").mkdir(parents=True)
    (folder / "audio.ogg").write_bytes(b"ogg")
    (folder / "work" / "audio.pcm").write_bytes(b"\x00\x00" * 16000)
    tester = GraphTester(str(folder), **kwargs)
    tester.set_test_mode_single(ADDON, json.dumps({"summary_timeout_s": 2.0}))
    err = tester.run()
    assert err is None, err.error_message()
    return tester


def test_an_upload_runs_through_to_an_archived_record(tmp_path):
    tester = run(tmp_path)

    assert tester.final["state"] == "archived"
    assert tester.received[:2] == ["segment_audio", "transcribe"]
    with open(tmp_path / "m1" / "record.json", encoding="utf-8") as f:
        record = json.load(f)
    assert [u["text"] for u in record["topics"][0]["utterances"]] == [
        "開始吧。",
        "好。",
    ]
    assert (tmp_path / "m1" / "minutes.txt").exists()


def test_a_segmenter_that_refuses_ends_the_meeting_failed(tmp_path):
    tester = run(tmp_path, segmenter_ok=False)

    assert tester.final["state"] == "failed"
    assert "segment_audio" in tester.final["error"]
