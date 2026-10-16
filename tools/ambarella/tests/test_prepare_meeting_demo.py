#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""prepare_meeting_demo.sh on a faked board: all ok when everything is
ready; each thing not ready is its own FAIL; a meeting left mid-way with no
worker is named; --warm starts the meeting worker and waits for it.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import glob
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Sandbox, free_port, http_server, read  # noqa: E402  pylint: disable=wrong-import-position

SERVICES = "qwen35-vllm ten-api meeting-screen"


def json_server(test, port, body):
    """The screen's service, faked: every GET answers body."""
    data = json.dumps(body).encode("utf-8")

    class Screen(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # pylint: disable=invalid-name
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", port), Screen)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)


class Board(Sandbox):
    def __init__(self, test, ready=True, showcase_state="archived", served="show-1"):
        super().__init__(test, scripts=["prepare_meeting_demo.sh", "set_desktop_autologin.sh"])
        self.test = test
        self.api_port = free_port()
        self.screen_port = free_port()
        self.write(
            ".env",
            f"SERVER_PORT={self.api_port}\nMEETINGS_DIR={self.path('meetings')}\n"
            "MEETING_SCREEN_SHOWCASE=show-1\n",
        )
        if showcase_state:
            self.meeting("show-1", showcase_state)
        self.write("meetings/.keep", "")
        self.env.update(
            ENV_FILE=self.path(".env"),
            SCREEN_URL=f"http://127.0.0.1:{self.screen_port}",
            CHECK_PY=self.path("bin", "fake_py"),
            KIOSK_PROFILE=self.path("home", ".cache", "meeting-screen-browser"),
            SDDM_CONF_D=self.path("sddm.conf.d"),
            SDDM_CONF=self.path("sddm.conf"),
        )
        if ready:
            self.env.update(FAKE_ACTIVE=SERVICES, FAKE_SESSION_TYPE="wayland", FAKE_FONTS="1")
            json_server(test, self.screen_port, {"scene": "ready", "showcase_id": served})
            # The kiosk's browser: anything of the user's naming its profile.
            self.start([sys.executable, "-c", "import time; time.sleep(120)", "--profile", self.env["KIOSK_PROFILE"]])

    def meeting(self, meeting_id, state):
        self.write(f"meetings/{meeting_id}/work/state.json", json.dumps({"state": state}))

    def stand_in(self, name, *args):
        """A process of the user's called name (pgrep -x), or naming args
        on its command line (pgrep -f)."""
        exe = self.path("bin", name)
        if not os.path.exists(exe):
            os.symlink(os.path.realpath(sys.executable), exe)
        self.start([exe, "-c", "import time; time.sleep(120)", *args])

    def prepare(self, *args):
        return self.run(["bash", self.path("prepare_meeting_demo.sh"), *args], timeout=120)


def fails(r):
    return [line for line in r.stdout.splitlines() if line.startswith("  FAIL")]


class Prepare(unittest.TestCase):
    def test_a_ready_board_is_all_ok(self):
        r = Board(self).prepare()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(fails(r), [])
        self.assertIn("showcase show-1 is there and archived", r.stdout)

    def test_each_thing_not_ready_is_its_own_fail(self):
        b = Board(self, ready=False, showcase_state=None)
        r = b.prepare()
        self.assertEqual(r.returncode, 1)
        text = "\n".join(fails(r))
        for expected in (
            "qwen35-vllm is not running",
            "meeting-screen is not running",
            "nothing answers at",
            "no desktop session",
            "the kiosk's browser is not running",
            "no Traditional Chinese font",
            "showcase show-1: no such meeting",
        ):
            self.assertIn(expected, text)

    def test_a_showcase_not_archived_is_a_fail(self):
        r = Board(self, showcase_state="failed").prepare()
        self.assertIn("showcase show-1 is failed, not archived", "\n".join(fails(r)))

    def test_a_meeting_left_midway_with_no_worker_is_named(self):
        b = Board(self)
        b.meeting("m2", "transcribing")
        r = b.prepare()
        self.assertEqual(r.returncode, 1)
        line = "\n".join(fails(r))
        self.assertIn("m2 (transcribing)", line)
        # The worker --warm starts marks it failed (store.fail_interrupted);
        # clear_meeting_worker.sh leaves its state as it is.
        self.assertIn("--warm", line)

    def test_a_meeting_queued_from_before_is_a_fail(self):
        b = Board(self)
        b.meeting("q1", "queued")
        r = b.prepare()
        self.assertEqual(r.returncode, 1)
        self.assertIn("q1", "\n".join(fails(r)))

    def test_a_showcase_the_screen_does_not_show_is_a_fail(self):
        r = Board(self, served=None).prepare()
        self.assertEqual(r.returncode, 1)
        line = "\n".join(fails(r))
        self.assertIn("show-1", line)
        self.assertIn("install_board_services.sh", line)

    def test_on_kde_the_browser_is_held_by_kde_inhibit(self):
        b = Board(self)
        b.stand_in("plasmashell")
        r = b.prepare()
        self.assertEqual(r.returncode, 1)
        self.assertIn("kde-inhibit", "\n".join(fails(r)))
        b.stand_in("holder", "kde-inhibit", "--power", "--screenSaver", "--", "firefox", "--profile", b.env["KIOSK_PROFILE"])
        r = b.prepare()
        self.assertEqual(fails(r), [], r.stdout)

    def test_check_meeting_board_failing_is_a_fail(self):
        b = Board(self)
        b.write("bin/failing_py", "#!/bin/sh\necho '  FAIL  no model'\nexit 1\n", 0o755)
        b.env["CHECK_PY"] = b.path("bin", "failing_py")
        r = b.prepare()
        self.assertIn("check_meeting_board.py --check failed", "\n".join(fails(r)))

    def test_warm_starts_the_worker_and_waits_for_it(self):
        b = Board(self)
        uploader = int(b.env["MEETING_UPLOADER"].rsplit(":", 1)[1])
        started = []
        test = self

        class Api(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):  # pylint: disable=invalid-name
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                started.append(body)
                # As the worker does once it holds 8765 (fail_interrupted).
                b.meeting("m2", "failed")
                http_server(test, uploader)
                data = b'{"code": "0"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        b.meeting("m2", "transcribing")
        api = ThreadingHTTPServer(("127.0.0.1", b.api_port), Api)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        r = b.prepare("--warm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            {k: started[0][k] for k in ("channel_name", "graph_name", "timeout")},
            {"channel_name": "meeting-room", "graph_name": "meeting_minutes", "timeout": 1800},
        )
        self.assertIn("the meeting worker is up", r.stdout)


class Log(unittest.TestCase):
    def test_what_it_says_is_kept_in_a_log_of_the_day(self):
        b = Board(self)
        r = b.prepare()
        (log,) = glob.glob(b.path("prepare_meeting_demo_[0-9]*_[0-9]*.log"))
        self.assertIn("ready for the demo", read(log))
        self.assertIn(f"log: {log}", r.stdout)


if __name__ == "__main__":
    unittest.main()
