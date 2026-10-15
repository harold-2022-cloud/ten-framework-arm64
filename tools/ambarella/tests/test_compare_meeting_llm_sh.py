#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""compare_meeting_llm.sh asks the 7B first and Qwen3.5-4B second, then
puts the board back on the LLM it served before: Qwen3.5-4B since the
switch, the 7B before it.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Sandbox  # noqa: E402  pylint: disable=wrong-import-position


class Board(Sandbox):
    def __init__(self, test, which):
        super().__init__(test, scripts=["compare_meeting_llm.sh"])
        self.install_qwen()
        self.env.update(PY=self.path("bin", "fake_py"), SERVE=self.path("bin", "fake_serve"), FAKE_WHICH=which)
        test.assertTrue(self.start_7b())  # what round 1 asks

    def compare(self):
        return self.run(["bash", self.path("compare_meeting_llm.sh")])

    def serve_calls(self):
        return [c[len("serve "):] for c in self.calls() if c.startswith("serve ")]


class PutBack(unittest.TestCase):
    def test_from_qwen_the_7b_answers_first_and_qwen_is_put_back(self):
        b = Board(self, "qwen")
        r = b.compare()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(b.serve_calls(), ["which", "stop", "start", "back qwen"])

    def test_from_the_7b_the_7b_is_put_back(self):
        b = Board(self, "7b")
        r = b.compare()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(b.serve_calls(), ["which", "start", "back 7b"])


if __name__ == "__main__":
    unittest.main()
