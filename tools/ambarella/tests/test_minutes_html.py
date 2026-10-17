#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""minutes_html.py: a meeting's record.json as one self-contained HTML page
-- the conclusion first, its numbered items as a list, who spoke how much and
when, every topic with its summary and transcript; nothing loaded from
anywhere, every text escaped.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import minutes_html  # noqa: E402  pylint: disable=wrong-import-position

RECORD = {
    "meeting_id": "pc-1",
    "title": "週會",
    "recorded_at": None,
    "duration_s": 600.0,
    "speaker_count": 2,
    "summary": "一、會議結論\n決定週三上線。\n\n二、待辦事項\n1. 說話人 1 準備測試。\n2. 說話人 2 寫文件。",
    "topics": [
        {
            "id": "t01",
            "start_s": 0.0,
            "end_s": 300.0,
            "summary": "討論上線時間。",
            "error": None,
            "utterances": [
                {"start_s": 1.0, "end_s": 61.0, "speaker": 0, "text": "我們週三上線。"},
                {"start_s": 70.0, "end_s": 100.0, "speaker": 1, "text": "<script>alert(1)</script>好。"},
            ],
        },
        {
            "id": "t02",
            "start_s": 300.0,
            "end_s": 600.0,
            "summary": "",
            "error": "ASR timed out",
            "utterances": [],
        },
    ],
    "actions": [],
    "actions_error": None,
    "error": None,
}


class Render(unittest.TestCase):
    def setUp(self):
        self.page = minutes_html.render(RECORD)

    def test_the_conclusion_comes_first_with_its_items_as_a_list(self):
        conclusion = self.page.index("決定週三上線。")
        self.assertLess(conclusion, self.page.index("討論上線時間。"))
        self.assertIn("<h3>會議結論</h3>", self.page)
        self.assertIn("<h3>待辦事項</h3>", self.page)
        self.assertRegex(self.page, r"<ol>\s*<li>說話人 1 準備測試。</li>\s*<li>說話人 2 寫文件。</li>\s*</ol>")

    def test_speakers_are_numbered_from_one_as_the_minutes_name_them(self):
        self.assertIn("說話人 1", self.page)
        self.assertIn("說話人 2", self.page)
        self.assertNotIn("說話人 0", self.page)
        self.assertIn("發言 2 人", self.page)

    def test_who_spoke_how_much(self):
        # 60 s and 30 s of 90.
        self.assertIn("66.7%", self.page)
        self.assertIn("33.3%", self.page)

    def test_every_topic_with_its_time_its_summary_and_its_transcript(self):
        self.assertIn("話題 1 · 00:00–05:00", self.page)
        self.assertIn("話題 2 · 05:00–10:00", self.page)
        self.assertIn("[00:01]", self.page)
        self.assertIn("我們週三上線。", self.page)

    def test_a_failed_topic_says_why(self):
        self.assertIn("ASR timed out", self.page)

    def test_text_is_escaped_and_nothing_is_loaded_from_outside(self):
        self.assertNotIn("<script>alert", self.page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;好。", self.page)
        self.assertIsNone(re.search(r'(src|href)="?https?:', self.page))
        self.assertNotIn("<link", self.page)

    def test_a_record_without_a_conclusion_says_so(self):
        page = minutes_html.render({**RECORD, "summary": ""})
        self.assertIn("整場結論未能產出", page)


class Command(unittest.TestCase):
    def test_it_writes_minutes_html_beside_the_record(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        record = os.path.join(folder, "record.json")
        with open(record, "w", encoding="utf-8") as f:
            import json  # pylint: disable=import-outside-toplevel

            json.dump(RECORD, f, ensure_ascii=False)
        r = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(HERE), "minutes_html.py"), record],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        out = os.path.join(folder, "minutes.html")
        with open(out, encoding="utf-8") as f:
            self.assertIn("週會", f.read())
        self.assertIn(out, r.stdout)


if __name__ == "__main__":
    unittest.main()
