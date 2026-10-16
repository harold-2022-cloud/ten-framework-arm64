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
        )
        self.write("tmp/.keep", "")

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
        self.assertEqual(
            read(os.path.join(services, "task_run_meeting.log")),
            "[meeting_control_python] topic t01 (720 s): diarized 300.0 s\n"
            "[meeting_transcriber] segment t02 failed\n",
        )
        self.assertIn("prepare_meeting_demo_20261016_120000.log", r.stdout)

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
