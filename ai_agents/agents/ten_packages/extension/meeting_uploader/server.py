#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The uploader's HTTP side: one meeting in, its status and archive out.

    POST /meeting/upload            multipart: file + optional fields
    GET  /meeting/{id}              state, progress, the record when done
    GET  /meeting/{id}/{name}       record.json | minutes.txt | audio.ogg
    GET  /meetings                  every meeting on the board

An upload is judged while the client is still connected -- 413, 415, 507
come back before the uploader lets go -- because once it has been handed
on, a refusal reaches nobody.
"""

import hmac
import os
import tempfile
import time
from typing import Awaitable, Callable, Optional

from aiohttp import web

from . import store
from .config import MeetingUploaderConfig

# Hands a landed meeting to the controller; returns why that failed, or None.
Emit = Callable[[dict], Awaitable[Optional[str]]]

CHUNK = 64 * 1024
# Multipart boundaries and the text fields ride on top of the file.
FORM_SLACK = 64 * 1024

FILES = {
    "record.json": "application/json",
    "minutes.txt": "text/plain; charset=utf-8",
    "audio.ogg": "audio/ogg",
}


UPLOADS = web.AppKey("uploads", "Uploads")


class _TooLarge(Exception):
    pass


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


class Uploads:
    """What the uploader holds between requests: the one meeting the
    controller is on. The controller takes one at a time -- the board's LLM
    serves one user -- so a second is refused here, before its file is
    sent, rather than accepted and failed a moment later."""

    def __init__(
        self,
        config: MeetingUploaderConfig,
        emit: Emit,
        free_mb: Callable[[str], float] = store.free_mb,
        now: Callable[[], float] = time.time,
        log: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.config = config
        self.emit = emit
        self.free_mb = free_mb
        self.now = now
        self.log = log
        self.processing: Optional[str] = None
        self.receiving = False
        self.incoming = os.path.join(config.meetings_dir, ".incoming")
        os.makedirs(self.incoming, exist_ok=True)

    def folder(self, meeting_id: str) -> str:
        return os.path.join(self.config.meetings_dir, meeting_id)

    def busy_with(self) -> Optional[str]:
        """The meeting still being processed, if any. Read off its state on
        disk, which the controller writes, so nothing has to tell the
        uploader a meeting is done."""
        if self.processing is not None:
            state = store.read_state(self.folder(self.processing))
            if state is None or state.get("state") in store.TERMINAL:
                self.processing = None
        return self.processing

    def busy(self) -> bool:
        return self.receiving or self.busy_with() is not None

    # --- POST /meeting/upload ------------------------------------------

    async def upload(self, request: web.Request) -> web.Response:
        busy = self.busy_with()
        if busy is not None:
            return _error(409, f"meeting {busy} is still being processed")
        if self.receiving:
            return _error(409, "another upload is being received")

        limit = self.config.max_upload_mb * store.MB
        length = request.content_length or 0
        if length > limit + FORM_SLACK:
            return self._too_large()
        free = self.free_mb(self.config.meetings_dir)
        if free < self.config.min_free_mb + length / store.MB:
            return _error(
                507,
                f"{free:.0f} MB free on the board; a meeting needs "
                f"{self.config.min_free_mb:.0f} MB beyond its upload",
            )

        if request.content_type != "multipart/form-data":
            return _error(400, "send multipart/form-data with a file field")

        self.receiving = True
        fd, tmp = tempfile.mkstemp(dir=self.incoming, suffix=".ogg")
        os.close(fd)
        try:
            return await self._take(request, tmp, limit)
        except _TooLarge:
            return self._too_large()
        finally:
            self.receiving = False
            if os.path.exists(tmp):
                os.remove(tmp)

    def _too_large(self) -> web.Response:
        return _error(
            413, f"over {self.config.max_upload_mb:g} MB; upload one Ogg-Opus"
        )

    async def _take(
        self, request: web.Request, tmp: str, limit: float
    ) -> web.Response:
        fields, size = await self._receive(request, tmp, limit)
        if size is None:
            return _error(400, "no file field")
        try:
            meeting_id, details = self._parse(fields)
        except ValueError as bad:
            return _error(400, str(bad))
        reason = store.check_audio(tmp)
        if reason is not None:
            self.log(f"refused an upload: {reason}")
            return _error(415, reason)

        folder = self.folder(meeting_id)
        ogg_path, work_dir = store.land(tmp, folder)
        store.write_received(
            folder, meeting_id, details["title"], received_at=self.now()
        )
        self.processing = meeting_id
        self.log(f"meeting {meeting_id} landed: {size} bytes")

        failure = await self.emit(
            {
                "meeting_id": meeting_id,
                "ogg_path": ogg_path,
                "work_dir": work_dir,
                **details,
            }
        )
        if failure is not None:
            message = f"could not hand the meeting on: {failure}"
            store.write_state(folder, state="failed", error=message)
            self.processing = None
            return _error(503, message)
        return web.json_response(
            {"meeting_id": meeting_id, "bytes": size, "status": "accepted"}
        )

    async def _receive(self, request: web.Request, tmp: str, limit: float):
        """The text fields, and how many bytes the file part had -- None
        when there was none. The file streams to disk; it is never whole in
        memory."""
        fields = {}
        size = None
        reader = await request.multipart()
        async for part in reader:
            if part.name == "file" and size is None:
                size = 0
                with open(tmp, "wb") as out:
                    while chunk := await part.read_chunk(CHUNK):
                        size += len(chunk)
                        if size > limit:
                            raise _TooLarge()
                        out.write(chunk)
            elif part.name:
                fields[part.name] = await part.text()
        return fields, size

    def _parse(self, fields: dict):
        meeting_id = fields.get("meeting_id") or store.new_id(self.now())
        if not store.valid_id(meeting_id):
            raise ValueError(
                "meeting_id: letters, digits, - and _ only, at most 64, "
                "starting with a letter or digit"
            )
        try:
            speakers = (
                int(fields["speakers"]) if fields.get("speakers") else None
            )
            recorded_at = (
                float(fields["recorded_at"])
                if fields.get("recorded_at")
                else None
            )
        except ValueError as bad:
            raise ValueError(f"speakers / recorded_at: {bad}") from bad
        if speakers is not None and speakers < 1:
            raise ValueError("speakers: at least 1")
        return meeting_id, {
            "title": fields.get("title") or None,
            "speakers": speakers,
            "recorded_at": recorded_at,
        }

    # --- reading back ---------------------------------------------------

    async def status(self, request: web.Request) -> web.Response:
        meeting_id = request.match_info["meeting_id"]
        found = (
            store.status(self.folder(meeting_id), meeting_id)
            if store.valid_id(meeting_id)
            else None
        )
        if found is None:
            return _error(404, f"no meeting {meeting_id}")
        return web.json_response(found)

    async def file(self, request: web.Request) -> web.StreamResponse:
        meeting_id = request.match_info["meeting_id"]
        name = request.match_info["name"]
        if not store.valid_id(meeting_id) or name not in FILES:
            return _error(404, f"no {name} for meeting {meeting_id}")
        path = os.path.join(self.folder(meeting_id), name)
        if not os.path.isfile(path):
            return _error(404, f"no {name} for meeting {meeting_id}")
        return web.FileResponse(path, headers={"Content-Type": FILES[name]})

    async def meetings(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {"meetings": store.list_meetings(self.config.meetings_dir)}
        )


def _guard(token: str):
    expected = f"Bearer {token}".encode()

    @web.middleware
    async def guard(request: web.Request, handler):
        if token:
            given = request.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(given, expected):
                return _error(401, "missing or wrong Authorization token")
        return await handler(request)

    return guard


def make_app(
    config: MeetingUploaderConfig,
    emit: Emit,
    free_mb: Callable[[str], float] = store.free_mb,
    now: Callable[[], float] = time.time,
    log: Callable[[str], None] = lambda _: None,
) -> web.Application:
    uploads = Uploads(config, emit, free_mb=free_mb, now=now, log=log)
    app = web.Application(middlewares=[_guard(config.auth_token)])
    app[UPLOADS] = uploads
    app.router.add_post("/meeting/upload", uploads.upload)
    app.router.add_get("/meetings", uploads.meetings)
    app.router.add_get("/meeting/{meeting_id}", uploads.status)
    app.router.add_get("/meeting/{meeting_id}/{name}", uploads.file)
    return app
