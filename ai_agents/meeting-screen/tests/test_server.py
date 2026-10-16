#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""server.py: the page and its files, /api/view and /api/meetings; no path
leaves the static folder, a pin is an id or a 400, an unknown one a 404;
python3 -m meeting_screen serves on 127.0.0.1:8766 unless told."""

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from conftest import SCREEN
from meeting_screen import server
from meeting_screen.__main__ import parser


def put(root, meeting_id, state, received_at, record=None):
    folder = os.path.join(root, meeting_id)
    os.makedirs(os.path.join(folder, "work"))
    with open(
        os.path.join(folder, "work", "state.json"), "w", encoding="utf-8"
    ) as f:
        json.dump({"state": state, "received_at": received_at}, f)
    if record is not None:
        with open(
            os.path.join(folder, "record.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(record, f, ensure_ascii=False)


@pytest.fixture
def port(tmp_path, record):
    put(str(tmp_path), "m1", "archived", time.time() - 60, record)
    httpd = server.make_server(server.Screen(str(tmp_path)), "127.0.0.1:0")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(port, path):
    # http.client sends the path as it is: nothing normalises a "..".
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    answer = conn.getresponse()
    body = answer.read()
    conn.close()
    return answer.status, answer.getheader("Content-Type") or "", body


def test_the_page_and_its_files(port):
    status, kind, body = get(port, "/")
    assert (status, kind.split(";")[0]) == (200, "text/html")
    assert "N1-655" in body.decode("utf-8")
    for name in server.FILES:
        if os.path.exists(os.path.join(server.STATIC, name)):
            assert get(port, f"/static/{name}")[0] == 200, name


def test_static_paths_cannot_leave_the_folder(port):
    for path in (
        "/static/../server.py",
        "/static/..%2Fserver.py",
        "/static/%2E%2E%2Fserver.py",
        "/static/../../meeting_screen/disk.py",
        "/static/",
        "/static/nope.js",
    ):
        assert get(port, path)[0] == 404, path


def test_the_view(port):
    status, kind, body = get(port, "/api/view")
    v = json.loads(body)
    assert (status, kind.split(";")[0]) == (200, "application/json")
    assert (v["scene"], v["meeting"]["meeting_id"]) == ("result", "m1")
    assert v["meeting"]["record"]["summary"] == "決定週三開會。"


def test_a_pin_that_cannot_be_an_id_is_400(port):
    for pin in ("..%2F..", "..", "a%2Fb", "%2E%2E"):
        assert get(port, f"/api/view?pin={pin}")[0] == 400, pin


def test_an_unknown_pin_is_404_and_a_known_one_pins(port):
    assert get(port, "/api/view?pin=nosuch")[0] == 404
    status, _, body = get(port, "/api/view?pin=m1")
    assert (status, json.loads(body)["pinned"]) == (200, True)


def test_the_meetings(port):
    status, _, body = get(port, "/api/meetings")
    assert status == 200
    assert [m["meeting_id"] for m in json.loads(body)] == ["m1"]


def test_anything_else_is_404(port):
    for path in ("/api", "/api/view/m1", "/index.html", "/favicon.ico"):
        assert get(port, path)[0] == 404, path


def test_localhost_8766_unless_told():
    args = parser().parse_args(["--meetings-dir", "/x"])
    assert (args.listen, args.recent_min, args.showcase) == (
        "127.0.0.1:8766",
        60.0,
        None,
    )


def test_python3_m_meeting_screen_serves(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "meeting_screen",
            "--meetings-dir",
            str(tmp_path),
            "--listen",
            f"127.0.0.1:{free}",
        ],
        cwd=SCREEN,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    status, body = None, b"{}"
    try:
        for _ in range(50):
            try:
                status, _, body = get(free, "/api/view")
                break
            except OSError:
                time.sleep(0.1)
        assert (status, json.loads(body).get("scene")) == (200, "ready")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
