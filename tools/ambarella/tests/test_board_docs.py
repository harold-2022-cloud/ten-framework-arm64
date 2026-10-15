#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The board's guides set the board up as it runs now: their first steps
start Qwen3.5-4B, not the 7B by hand (the two would share the NPU), and the
defaults they list are the graphs' own.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
QUICKSTARTS = {
    "docs/development/board_quickstart.md": "## Before you start",
    "docs/development/board_quickstart.zh-TW.md": "## 開始之前",
}
EXAMPLE = "ai_agents/agents/examples/voice-assistant-ambarella/README.md"
START_7B = "run_llm_demo.sh --run_mode start"


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


def section(text, heading):
    start = text.index(heading + "\n")
    end = text.find("\n## ", start + len(heading))
    return text[start:end if end >= 0 else len(text)]


class Guides(unittest.TestCase):
    def test_the_first_steps_start_qwen_not_the_7b(self):
        for rel, heading in QUICKSTARTS.items():
            first = section(read(rel), heading)
            self.assertIn("serve_qwen35.sh start", first, rel)
            self.assertNotIn(START_7B, first, rel)

    def test_the_listed_default_is_the_graphs_own(self):
        for rel in QUICKSTARTS:
            text = read(rel)
            self.assertIn("| `QWEN_LLM_BASE_URL` | `http://127.0.0.1:8000/v1` |", text, rel)
            self.assertNotIn("| `AMBARELLA_LLM_BASE_URL` | `http://127.0.0.1:8080` |", text, rel)

    def test_going_back_to_the_7b_is_not_undone_by_the_next_install(self):
        for rel in QUICKSTARTS:
            self.assertIn("install_board_services.sh --uninstall", read(rel), rel)

    def test_the_example_starts_qwen_not_the_7b(self):
        text = read(EXAMPLE)
        self.assertIn("serve_qwen35.sh start", text)
        self.assertNotIn(START_7B, text)


if __name__ == "__main__":
    unittest.main()
