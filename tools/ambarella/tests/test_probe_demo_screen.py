#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""probe_demo_screen.py keeps its log under the day's name, where
pull_board_check.sh looks: LOG_DIR, else the temporary directory.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probe_demo_screen as probe  # noqa: E402  pylint: disable=wrong-import-position


class LogPath(unittest.TestCase):
    def test_named_by_the_day_and_time_in_log_dir(self):
        at = time.mktime((2026, 10, 16, 12, 10, 5, 0, 0, -1))
        with mock.patch.dict(os.environ, {"LOG_DIR": "/somewhere"}):
            self.assertEqual(
                probe.log_path(at), "/somewhere/probe_demo_screen_20261016_121005.log"
            )

    def test_the_temporary_directory_without_log_dir(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            path = probe.log_path()
        self.assertEqual(os.path.dirname(path), tempfile.gettempdir())
        self.assertRegex(os.path.basename(path), r"^probe_demo_screen_\d{8}_\d{6}\.log$")


if __name__ == "__main__":
    unittest.main()
