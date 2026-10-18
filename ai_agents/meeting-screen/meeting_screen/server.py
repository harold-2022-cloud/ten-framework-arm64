#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The screen's HTTP service: the page, and what it shows as JSON.

    GET /                 the page
    GET /static/<file>    its script and style, by name only
    GET /api/view?pin=ID  what to show now (view.build); 400 for a pin
                          that cannot be an id, 404 for an unknown one
    GET /api/meetings     every meeting, the newest received first

Listens on 127.0.0.1 unless told otherwise: the transcripts it serves are
behind no token.
"""

import json
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional, Tuple

from . import disk, view

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
FILES = {
    "index.html": "text/html; charset=utf-8",
    "screen.css": "text/css; charset=utf-8",
    "screen.mjs": "text/javascript; charset=utf-8",
    "screen_model.mjs": "text/javascript; charset=utf-8",
}


def boot_time(path: str = "/proc/stat") -> Optional[float]:
    """When this machine booted (the kernel's btime), or None."""
    try:
        with open(path, encoding="ascii") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


class Screen:
    def __init__(
        self,
        meetings_dir: str,
        showcase: Optional[str] = None,
        recent_min: float = 120.0,
        clock=time.time,
        boot: Optional[Callable[[], Optional[float]]] = None,
    ):
        self.meetings_dir = meetings_dir
        self.showcase = showcase or None
        self.recent_min = recent_min
        self.clock = clock
        # When the board booted, asked at each request: the kernel moves
        # btime when the clock is stepped (NTP after a cold boot with no
        # clock battery), and the meetings' times are on the new clock.
        # None: no meeting is taken as cut off by a reboot (view.settle).
        self.boot = boot

    def booted_at(self) -> Optional[float]:
        return self.boot() if self.boot is not None else None

    def view(self, pin: Optional[str] = None) -> Tuple[int, object]:
        if pin is not None:
            if not disk.valid_id(pin):
                return 400, {"error": "not a meeting id"}
            if disk.read_meeting(self.meetings_dir, pin) is None:
                return 404, {"error": "no such meeting"}
        meetings = disk.list_meetings(self.meetings_dir)
        return 200, view.build(
            meetings,
            lambda meeting_id: disk.read_record(self.meetings_dir, meeting_id),
            self.clock(),
            self.showcase,
            pin,
            self.recent_min,
            self.booted_at(),
        )

    def meetings(self) -> Tuple[int, object]:
        return 200, view.listing(
            disk.list_meetings(self.meetings_dir),
            self.showcase,
            lambda meeting_id: disk.read_record(self.meetings_dir, meeting_id),
            self.booted_at(),
        )


def handler_for(screen: Screen):
    class Handler(BaseHTTPRequestHandler):
        server_version = "meeting-screen"

        def log_message(self, *_):
            pass

        def _send(self, status, body: bytes, kind: str, cache: str):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self._send(
                status, body, "application/json; charset=utf-8", "no-store"
            )

        def _file(self, name):
            with open(os.path.join(STATIC, name), "rb") as f:
                self._send(200, f.read(), FILES[name], "no-cache")

        def do_GET(self):  # pylint: disable=invalid-name
            parts = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parts.path)
            if path == "/":
                self._file("index.html")
            elif path.startswith("/static/") and path[8:] in FILES:
                self._file(path[8:])
            elif path == "/api/view":
                query = urllib.parse.parse_qs(parts.query)
                pin = (query.get("pin") or [""])[0] or None
                self._json(*screen.view(pin))
            elif path == "/api/meetings":
                self._json(*screen.meetings())
            else:
                self._json(404, {"error": "not found"})

    return Handler


def make_server(
    screen: Screen, listen: str = "127.0.0.1:8766"
) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    httpd = ThreadingHTTPServer(
        (host or "127.0.0.1", int(port)), handler_for(screen)
    )
    httpd.daemon_threads = True
    return httpd
