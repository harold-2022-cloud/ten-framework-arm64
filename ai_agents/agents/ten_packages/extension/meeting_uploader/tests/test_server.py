#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The HTTP side, through a real aiohttp server on a free port.

The controller is played by a fake emit(): it records what the uploader
would send as meeting_uploaded, and says whether sending worked.
"""

import io

import numpy as np
import pytest
import soundfile as sf
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from meeting_uploader import store
from meeting_uploader.config import MeetingUploaderConfig
from meeting_uploader.server import UPLOADS, make_app


def ogg_bytes(rate=16000, fmt="OGG", subtype="OPUS", seconds=1.0):
    t = np.arange(int(seconds * rate)) / rate
    buf = io.BytesIO()
    sf.write(
        buf,
        0.3 * np.sin(2 * np.pi * 440 * t),
        rate,
        format=fmt,
        subtype=subtype,
    )
    return buf.getvalue()


AUDIO = ogg_bytes()


class Controller:
    def __init__(self, error=None):
        self.uploaded = []
        self.error = error

    async def __call__(self, payload):
        self.uploaded.append(payload)
        return self.error


def form(data=AUDIO, **fields):
    f = FormData()
    for key, value in fields.items():
        f.add_field(key, str(value))
    if data is not None:
        f.add_field("file", data, filename="meeting.ogg")
    return f


class Clock:
    """A second later at every look, so arrivals have an order."""

    def __init__(self):
        self.t = 1790663400.0

    def __call__(self):
        self.t += 1.0
        return self.t


def client(tmp_path, controller=None, free=10_000.0, now=None, **settings):
    config = MeetingUploaderConfig(meetings_dir=str(tmp_path), **settings)
    app = make_app(
        config,
        controller or Controller(),
        free_mb=lambda _: free,
        now=now or (lambda: 1790663400.0),
    )
    return TestClient(TestServer(app))


def landed(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if p.name != ".incoming")


def incoming(tmp_path):
    folder = tmp_path / ".incoming"
    return list(folder.iterdir()) if folder.exists() else []


@pytest.mark.asyncio
async def test_an_upload_lands_byte_for_byte_and_tells_the_controller(
    tmp_path,
):
    controller = Controller()
    async with client(tmp_path, controller) as c:
        r = await c.post(
            "/meeting/upload",
            data=form(
                meeting_id="m1", title="週會", speakers=4, recorded_at=1.5e9
            ),
        )
        body = await r.json()

    assert r.status == 200
    assert body == {
        "meeting_id": "m1",
        "bytes": len(AUDIO),
        "status": "accepted",
        "ahead": 0,
        "wait_s": 0,
    }
    assert (tmp_path / "m1" / "audio.ogg").read_bytes() == AUDIO
    assert controller.uploaded == [
        {
            "meeting_id": "m1",
            "ogg_path": str(tmp_path / "m1" / "audio.ogg"),
            "work_dir": str(tmp_path / "m1" / "work"),
            "title": "週會",
            "speakers": 4,
            "recorded_at": 1.5e9,
            "script": None,
        }
    ]
    assert store.read_state(str(tmp_path / "m1"))["state"] == "received"
    assert incoming(tmp_path) == []


@pytest.mark.asyncio
async def test_without_an_id_the_arrival_time_names_the_meeting(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "new_id", lambda now: f"at-{int(now)}")
    controller = Controller()
    async with client(tmp_path, controller) as c:
        r = await c.post("/meeting/upload", data=form())
        body = await r.json()

    assert body["meeting_id"] == "at-1790663400"
    assert controller.uploaded[0]["speakers"] is None
    assert controller.uploaded[0]["title"] is None


@pytest.mark.asyncio
async def test_the_chosen_script_reaches_the_controller(tmp_path):
    controller = Controller()
    async with client(tmp_path, controller) as c:
        r = await c.post(
            "/meeting/upload", data=form(meeting_id="m1", script="traditional")
        )

    assert r.status == 200
    assert controller.uploaded[0]["script"] == "traditional"


@pytest.mark.asyncio
async def test_an_unknown_script_is_a_bad_request_naming_the_choices(tmp_path):
    async with client(tmp_path) as c:
        r = await c.post(
            "/meeting/upload", data=form(meeting_id="m1", script="cantonese")
        )
        body = await r.json()

    assert r.status == 400
    assert "traditional" in body["error"] and "simplified" in body["error"]
    assert landed(tmp_path) == []


@pytest.mark.asyncio
async def test_a_wav_is_refused_with_415_and_nothing_stays(tmp_path):
    controller = Controller()
    async with client(tmp_path, controller) as c:
        r = await c.post(
            "/meeting/upload",
            data=form(ogg_bytes(fmt="WAV", subtype="PCM_16"), meeting_id="m1"),
        )
        body = await r.json()

    assert r.status == 415
    assert "WAV" in body["error"]
    assert landed(tmp_path) == []
    assert incoming(tmp_path) == []
    assert controller.uploaded == []


@pytest.mark.asyncio
async def test_a_file_over_the_limit_is_refused_with_413(tmp_path):
    big = ogg_bytes(seconds=30.0)
    async with client(tmp_path, max_upload_mb=len(big) / 2 / store.MB) as c:
        r = await c.post("/meeting/upload", data=form(big, meeting_id="m1"))

    assert r.status == 413
    assert landed(tmp_path) == []
    assert incoming(tmp_path) == []


@pytest.mark.asyncio
async def test_too_little_disk_is_refused_with_507(tmp_path):
    async with client(tmp_path, free=100.0, min_free_mb=250.0) as c:
        r = await c.post("/meeting/upload", data=form(meeting_id="m1"))
        body = await r.json()

    assert r.status == 507
    assert "250" in body["error"]
    assert landed(tmp_path) == []


@pytest.mark.asyncio
async def test_an_upload_without_a_file_is_a_bad_request(tmp_path):
    async with client(tmp_path) as c:
        r = await c.post("/meeting/upload", data=form(None, title="週會"))

    assert r.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", [{"meeting_id": "../etc"}, {"speakers": "four"}]
)
async def test_a_field_that_does_not_parse_is_a_bad_request(tmp_path, field):
    async with client(tmp_path) as c:
        r = await c.post("/meeting/upload", data=form(**field))

    assert r.status == 400
    assert landed(tmp_path) == []
    assert incoming(tmp_path) == []


@pytest.mark.asyncio
async def test_a_meeting_sent_while_another_is_processed_is_queued(tmp_path):
    # Any phone, any time: the board's LLM serves one meeting at a time, so
    # the second waits its turn instead of being turned away.
    controller = Controller()
    async with client(tmp_path, controller) as c:
        await c.post("/meeting/upload", data=form(meeting_id="m1"))
        r = await c.post("/meeting/upload", data=form(meeting_id="m2"))
        body = await r.json()
        status = await (await c.get("/meeting/m2")).json()

    assert r.status == 200
    assert body["status"] == "queued"
    assert body["ahead"] == 1
    assert body["wait_s"] > 0
    assert status["state"] == "queued"
    assert status["queue"]["ahead"] == 1
    assert landed(tmp_path) == ["m1", "m2"]
    assert [p["meeting_id"] for p in controller.uploaded] == ["m1"]


@pytest.mark.asyncio
async def test_the_queue_moves_in_the_order_meetings_arrived(tmp_path):
    controller = Controller()
    async with client(tmp_path, controller, now=Clock()) as c:
        uploads = c.server.app[UPLOADS]
        for meeting_id in ("m1", "m3", "m2"):
            await c.post("/meeting/upload", data=form(meeting_id=meeting_id))
        before = (await (await c.get("/meeting/m2")).json())["queue"]

        assert await uploads.dispatch_next() is None  # m1 is still going
        store.write_state(str(tmp_path / "m1"), state="archived")
        assert await uploads.dispatch_next() == "m3"
        then = (await (await c.get("/meeting/m2")).json())["queue"]
        store.write_state(str(tmp_path / "m3"), state="empty")
        assert await uploads.dispatch_next() == "m2"
        assert await uploads.dispatch_next() is None

    assert before["ahead"] == 2
    assert then["ahead"] == 1
    assert then["wait_s"] < before["wait_s"]
    assert [p["meeting_id"] for p in controller.uploaded] == ["m1", "m3", "m2"]


@pytest.mark.asyncio
async def test_a_queued_meeting_reaches_the_controller_as_it_was_sent(
    tmp_path,
):
    controller = Controller()
    async with client(tmp_path, controller) as c:
        uploads = c.server.app[UPLOADS]
        await c.post("/meeting/upload", data=form(meeting_id="m1"))
        await c.post(
            "/meeting/upload",
            data=form(
                meeting_id="m2",
                speakers=4,
                title="週會",
                recorded_at=1790660000,
                script="traditional",
            ),
        )
        store.write_state(str(tmp_path / "m1"), state="archived")
        await uploads.dispatch_next()
        status = await (await c.get("/meeting/m2")).json()

    sent = controller.uploaded[1]
    assert sent["meeting_id"] == "m2"
    assert sent["ogg_path"] == str(tmp_path / "m2" / "audio.ogg")
    assert sent["work_dir"] == str(tmp_path / "m2" / "work")
    assert (
        sent["speakers"],
        sent["title"],
        sent["recorded_at"],
        sent["script"],
    ) == (4, "週會", 1790660000.0, "traditional")
    assert status["state"] == "received"
    assert status.get("queue") is None


@pytest.mark.asyncio
async def test_a_meeting_being_processed_cannot_be_replaced(tmp_path):
    async with client(tmp_path) as c:
        await c.post("/meeting/upload", data=form(meeting_id="m1"))
        r = await c.post("/meeting/upload", data=form(meeting_id="m1"))
        body = await r.json()

    assert r.status == 409
    assert "m1" in body["error"]


@pytest.mark.asyncio
async def test_a_queued_meeting_sent_again_still_waits(tmp_path):
    controller = Controller()
    async with client(tmp_path, controller) as c:
        await c.post("/meeting/upload", data=form(meeting_id="m1"))
        await c.post("/meeting/upload", data=form(meeting_id="m2"))
        r = await c.post("/meeting/upload", data=form(meeting_id="m2"))
        body = await r.json()

    assert r.status == 200
    assert body["status"] == "queued"
    assert landed(tmp_path) == ["m1", "m2"]
    assert [p["meeting_id"] for p in controller.uploaded] == ["m1"]


@pytest.mark.asyncio
async def test_a_queued_meeting_the_controller_cannot_take_fails_alone(
    tmp_path,
):
    controller = Controller()
    async with client(tmp_path, controller, now=Clock()) as c:
        uploads = c.server.app[UPLOADS]
        for meeting_id in ("m1", "m2", "m3"):
            await c.post("/meeting/upload", data=form(meeting_id=meeting_id))
        store.write_state(str(tmp_path / "m1"), state="archived")
        controller.error = "no route"
        assert await uploads.dispatch_next() == "m2"
        controller.error = None
        assert await uploads.dispatch_next() == "m3"

    state = store.read_state(str(tmp_path / "m2"))
    assert state["state"] == "failed"
    assert "no route" in state["error"]


@pytest.mark.asyncio
async def test_the_same_id_again_after_it_finished_starts_over(tmp_path):
    async with client(tmp_path) as c:
        await c.post("/meeting/upload", data=form(meeting_id="m1"))
        store.write_state(str(tmp_path / "m1"), state="archived")
        (tmp_path / "m1" / "record.json").write_text("{}", encoding="utf-8")

        r = await c.post("/meeting/upload", data=form(meeting_id="m1"))
        status = await (await c.get("/meeting/m1")).json()

    assert r.status == 200
    assert status["state"] == "received"
    assert status["record"] is None


@pytest.mark.asyncio
async def test_a_controller_that_cannot_be_reached_fails_the_meeting(
    tmp_path,
):
    async with client(tmp_path, Controller(error="no route")) as c:
        r = await c.post("/meeting/upload", data=form(meeting_id="m1"))
        body = await r.json()
        again = await c.post("/meeting/upload", data=form(meeting_id="m2"))

    assert r.status == 503
    assert "no route" in body["error"]
    state = store.read_state(str(tmp_path / "m1"))
    assert (state["state"], state["error"]) == ("failed", body["error"])
    assert again.status == 503  # not 409: nothing is being processed


@pytest.mark.asyncio
async def test_status_reports_progress_and_unknown_ids_are_404(tmp_path):
    async with client(tmp_path) as c:
        await c.post("/meeting/upload", data=form(meeting_id="m1"))
        store.write_state(
            str(tmp_path / "m1"),
            state="transcribing",
            topics_done=1,
            topics_total=5,
        )

        status = await (await c.get("/meeting/m1")).json()
        unknown = await c.get("/meeting/nobody")
        sneaky = await c.get("/meeting/..")

    assert status["state"] == "transcribing"
    assert status["progress"] == {"topics_done": 1, "topics_total": 5}
    assert unknown.status == 404
    assert sneaky.status == 404


@pytest.mark.asyncio
async def test_the_archive_can_be_fetched_and_nothing_else(tmp_path):
    async with client(tmp_path) as c:
        await c.post("/meeting/upload", data=form(meeting_id="m1"))
        (tmp_path / "m1" / "minutes.txt").write_text("週會", encoding="utf-8")

        audio = await c.get("/meeting/m1/audio.ogg")
        audio_bytes = await audio.read()
        minutes = await c.get("/meeting/m1/minutes.txt")
        minutes_text = await minutes.text()
        not_yet = await c.get("/meeting/m1/record.json")
        state = await c.get("/meeting/m1/work")

    assert audio_bytes == AUDIO
    assert audio.content_type == "audio/ogg"
    assert minutes_text == "週會"
    assert not_yet.status == 404
    assert state.status == 404


@pytest.mark.asyncio
async def test_meetings_are_listed(tmp_path):
    async with client(tmp_path) as c:
        await c.post(
            "/meeting/upload", data=form(meeting_id="m1", title="週會")
        )
        body = await (await c.get("/meetings")).json()

    assert body == {
        "meetings": [
            {
                "meeting_id": "m1",
                "state": "received",
                "title": "週會",
                "received_at": 1790663400.0,
            }
        ]
    }


@pytest.mark.asyncio
async def test_with_a_token_set_every_request_must_carry_it(tmp_path):
    async with client(tmp_path, auth_token="s3cret") as c:
        bare = await c.get("/meetings")
        wrong = await c.get(
            "/meetings", headers={"Authorization": "Bearer nope"}
        )
        upload = await c.post("/meeting/upload", data=form(meeting_id="m1"))
        right = await c.get(
            "/meetings", headers={"Authorization": "Bearer s3cret"}
        )

    assert (bare.status, wrong.status, upload.status) == (401, 401, 401)
    assert landed(tmp_path) == []
    assert right.status == 200


@pytest.mark.asyncio
async def test_busy_is_what_keeps_the_worker_alive(tmp_path):
    config = MeetingUploaderConfig(meetings_dir=str(tmp_path))
    app = make_app(config, Controller(), free_mb=lambda _: 10_000.0)
    uploads = app[UPLOADS]

    async with TestClient(TestServer(app)) as c:
        assert uploads.busy_with() is None
        assert not uploads.busy()
        await c.post("/meeting/upload", data=form(meeting_id="m1"))
        assert uploads.busy_with() == "m1"
        await c.post("/meeting/upload", data=form(meeting_id="m2"))
        store.write_state(str(tmp_path / "m1"), state="empty")
        assert uploads.busy_with() is None
        assert uploads.busy()  # m2 is still waiting its turn
        await uploads.dispatch_next()
        store.write_state(str(tmp_path / "m2"), state="archived")
        assert not uploads.busy()
