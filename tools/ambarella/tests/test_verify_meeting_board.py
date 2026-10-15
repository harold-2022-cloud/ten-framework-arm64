#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""verify_meeting_board.sh leaves the board on the LLM it served: Qwen3.5-4B
serving is left serving; a board switched from the 7B gets the 7B back --
also after Ctrl-C, unless --keep-qwen; a board with no LLM keeps
Qwen3.5-4B, which every board graph asks. A meeting worker left up is
cleared before the switch.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import contextlib
import glob
import io
import os
import signal
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import TOOLS, Sandbox, http_server, read, wait_for  # noqa: E402  pylint: disable=wrong-import-position
from test_qwen35_prepare import package, prep  # noqa: E402  pylint: disable=wrong-import-position

VERIFY = os.path.join(TOOLS, "verify_meeting_board.sh")


class Board(Sandbox):
    def __init__(self, test, which, worker=False):
        # verify runs from the checkout (it reads its graphs and its git log)
        # and does not refuse root.
        super().__init__(test, board_user=False)
        os.makedirs(self.path("pkgs"))
        pkg = package(self.path("pkgs"), prep.GEN_NEW)
        with contextlib.redirect_stdout(io.StringIO()):
            prep.model(pkg, False, prep.VIT_NAME)  # nothing left to do
        self.env.update(
            PY="python3", QWEN_PKG=pkg, SYSTEM_DIR=self.path("none"),
            SERVE=self.path("bin", "fake_serve"), CLEAR=self.path("bin", "fake_clear"),
            LOG_DIR=self.root, FAKE_WHICH=which,
        )
        if worker:
            http_server(test, int(self.env["MEETING_UPLOADER"].rsplit(":", 1)[1]))

    def verify(self, *args):
        return self.run([VERIFY, "--no-meeting", "--graph", "meeting_minutes", *args])

    def serve_calls(self):
        return [c[len("serve "):] for c in self.calls() if c.startswith("serve ")]

    def log(self):
        return "".join(read(p) for p in glob.glob(self.path("verify_meeting_*.log")))


class LeftAsFound(unittest.TestCase):
    def test_qwen_serving_is_left_serving(self):
        b = Board(self, "qwen")
        r = b.verify()
        self.assertEqual(b.serve_calls(), ["which", "test"])
        self.assertNotIn("given back", r.stdout)

    def test_switched_from_the_7b_it_gives_the_7b_back(self):
        b = Board(self, "7b")
        self.assertTrue(b.start_7b())  # what the 7B looks like once given back
        r = b.verify()
        self.assertEqual(b.serve_calls(), ["which", "start", "test", "back 7b"])
        self.assertIn("ok    the DeepSeek 7B is back", r.stdout)

    def test_switched_from_nothing_qwen_is_left_serving(self):
        b = Board(self, "none")
        r = b.verify()
        self.assertEqual(b.serve_calls(), ["which", "start", "test"])
        self.assertIn("Qwen3.5-4B left serving", r.stdout)

    def test_keep_qwen_keeps_it(self):
        b = Board(self, "7b")
        r = b.verify("--keep-qwen")
        self.assertNotIn("back 7b", b.serve_calls())
        self.assertIn("--keep-qwen: Qwen3.5-4B left serving", r.stdout)

    def test_ctrl_c_still_gives_the_7b_back(self):
        b = Board(self, "7b")
        proc = b.start([VERIFY, "--no-meeting", "--graph", "meeting_minutes"], SLOW_TEST="20")
        self.assertTrue(wait_for(lambda: "serve test" in b.calls(), timeout=60))
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=60)
        self.assertIn("serve back 7b", b.calls())
        self.assertIn("(fake serve_qwen35.sh back, done)", b.log())

    def test_a_worker_left_up_is_cleared_before_the_switch(self):
        b = Board(self, "7b", worker=True)
        b.verify()
        calls = b.calls()
        self.assertLess(calls.index("clear"), calls.index("serve start"))


if __name__ == "__main__":
    unittest.main()
