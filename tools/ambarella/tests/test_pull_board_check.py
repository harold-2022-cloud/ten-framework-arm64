#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""pull_board_check.sh against a faked board (ssh runs the command here):
the day's meeting runs and every board tool's log of that day come back,
with the service logs as they are now -- the screen's, the kiosk's, and
the meeting worker's own lines from task_run.log -- and another day's stay.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Sandbox, read  # noqa: E402  pylint: disable=wrong-import-position

DAY_LOGS = (
    "verify_meeting_20261016_053931.log",
    "prepare_meeting_demo_20261016_120000.log",
    "install_board_services_20261016_115900.log",
    "probe_demo_screen_20261016_121000.log",
)


class Board(Sandbox):
    def __init__(self, test):
        super().__init__(test, scripts=["pull_board_check.sh"])
        self.env.update(
            BOARD="lychee@board",
            DEST=self.path("dest"),
            BOARD_LOG_DIR=self.path("tmp"),
            BOARD_REPO=self.path("repo"),
        )
        self.write("tmp/.keep", "")
        self.write(
            "repo/ai_agents/.env",
            f"LOG_PATH={self.path('ten_agent')}\nMEETINGS_DIR={self.path('meetings')}\n",
        )

    def pull(self, *args):
        return self.run(["bash", self.path("pull_board_check.sh"), *args], timeout=60)

    def pulled(self, *parts):
        return os.path.join(self.path("dest"), *parts)


class Pull(unittest.TestCase):
    def test_a_days_runs_logs_and_service_logs_come_back(self):
        b = Board(self)
        b.write("home/meeting_probe/board_check/20261016-053950/record.json", "{}")
        b.write("home/meeting_probe/board_check/20261015-215624/record.json", "{}")
        for name in DAY_LOGS + ("verify_meeting_20261015_215600.log",):
            b.write(f"tmp/{name}", name)
        b.write("tmp/meeting_screen.log", "screen\n")
        b.write("tmp/meeting_screen_kiosk.log", "kiosk\n")
        b.write(
            "tmp/task_run.log",
            "the server's own line\n"
            "[meeting_control_python] topic t01 (720 s): diarized 300.0 s\n"
            "another line\n"
            "[meeting_transcriber] segment t02 failed\n",
        )

        r = b.pull("20261016")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(b.pulled("20261016-053950", "record.json")))
        self.assertFalse(os.path.exists(b.pulled("20261015-215624")))
        for name in DAY_LOGS:
            self.assertEqual(read(b.pulled(name)), name)
        self.assertFalse(os.path.exists(b.pulled("verify_meeting_20261015_215600.log")))
        services = b.pulled("20261016_services")
        self.assertEqual(read(os.path.join(services, "meeting_screen.log")), "screen\n")
        self.assertEqual(read(os.path.join(services, "meeting_screen_kiosk.log")), "kiosk\n")
        self.assertIn("the server's own line", read(os.path.join(services, "task_run.log")))
        self.assertIn("prepare_meeting_demo_20261016_120000.log", r.stdout)

    def test_each_workers_own_log_and_every_meeting_on_the_board_come_back(self):
        # With LOG_STDOUT false the server gives each worker a file of its
        # own under LOG_PATH (server/internal/http_server.go:829).
        b = Board(self)
        b.write("tmp/verify_meeting_20261016_053931.log", "v")
        b.write("ten_agent/app-meeting-room-20261016_165900.log", "[meeting_uploader] refused\n")
        b.write(
            "meetings/m80/work/state.json",
            '{"state": "failed", "error": "boom", "received_at": 1760605200}',
        )
        b.write("meetings/m80/upload.json", '{"duration_s": 4800.0}')
        b.write("meetings/m80/audio.ogg", "x" * 1000)
        b.write("meetings/half/upload.json", "{}")

        r = b.pull("20261016")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        services = b.pulled("20261016_services")
        self.assertEqual(
            read(os.path.join(services, "workers", "app-meeting-room-20261016_165900.log")),
            "[meeting_uploader] refused\n",
        )
        meetings = read(os.path.join(services, "meetings.txt"))
        self.assertIn("m80  failed  80:00  1000 bytes  boom", meetings)
        self.assertIn("half  (no state)", meetings)

    def test_the_boards_uptime_memory_and_kernel_kills_come_back(self):
        b = Board(self)
        b.write("tmp/verify_meeting_20261016_053931.log", "v")
        b.write(
            "bin/journalctl",
            "#!/bin/sh\necho \"journalctl $*\" >> \"$FAKE_LOG\"\n"
            "echo 'kernel: Out of memory: Killed process 1234 (python3.12)'\n",
            0o755,
        )
        r = b.pull("20261016")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        system = read(os.path.join(b.pulled("20261016_services"), "system.txt"))
        for heading in ("== uptime", "== memory", "== kernel, this boot", "== kernel, the boot before"):
            self.assertIn(heading, system)
        self.assertIn("Killed process 1234", system)
        self.assertIn("journalctl -k -b -1", "\n".join(b.calls()))

    def test_logs_come_back_before_any_meeting_ran(self):
        b = Board(self)
        b.write("tmp/prepare_meeting_demo_20261016_120000.log", "ready")
        r = b.pull("20261016")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(read(b.pulled("prepare_meeting_demo_20261016_120000.log")), "ready")

    def test_nothing_that_day_says_so(self):
        b = Board(self)
        b.write("tmp/prepare_meeting_demo_20261015_120000.log", "ready")
        r = b.pull("20261016")
        self.assertEqual(r.returncode, 3)
        self.assertIn("nothing from 20261016", r.stderr)


if __name__ == "__main__":
    unittest.main()
