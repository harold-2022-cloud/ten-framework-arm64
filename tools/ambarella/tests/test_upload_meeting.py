#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""upload_meeting.py against a faked board: it starts the worker on the
app's channel (or pings one already running), waits for the uploader, sends
the file as the app does -- multipart, the app's fields -- says how fast it
went, and with --follow follows the meeting to its end.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import contextlib
import email.parser
import email.policy
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import upload_meeting  # noqa: E402  pylint: disable=wrong-import-position


class FakeBoard:
    """The API server and the meeting worker's uploader, on two ports."""

    def __init__(self, test, running=False, refuse=None, final_after=2):
        self.started = []
        self.pinged = []
        self.uploads = []
        self.polls = 0
        self.running = running
        self.refuse = refuse
        self.final_after = final_after
        board = self

        class Server(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):  # pylint: disable=invalid-name
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/start":
                    board.started.append(body)
                    code = "10003" if board.running else "0"
                    board.running = True
                    self._json(400 if code != "0" else 200, {"code": code})
                elif self.path == "/ping":
                    board.pinged.append(body)
                    self._json(200, {"code": "0"})

            def _json(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Uploader(Server):
            def do_GET(self):  # pylint: disable=invalid-name
                if self.path == "/meetings":
                    self._json(200, [])
                    return
                if self.path.endswith(("/record.json", "/minutes.txt")):
                    name = self.path.rsplit("/", 1)[1]
                    meeting_id = self.path.split("/")[2]
                    data = f"{name} of {meeting_id}".encode()
                    if name == "record.json":
                        data = json.dumps({"meeting_id": meeting_id, "title": f"週會 {meeting_id}", "summary": "結論", "topics": []}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                board.polls += 1
                state = "transcribing" if board.polls < board.final_after else "archived"
                self._json(200, {"state": state, "progress": {"topics_done": board.polls, "topics_total": 2}, "error": None})

            def do_POST(self):  # pylint: disable=invalid-name
                length = int(self.headers["Content-Length"])
                raw = self.rfile.read(length)
                head = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
                message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + raw)
                parts = {
                    p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                    for p in message.iter_parts()
                }
                board.uploads.append(parts)
                if board.refuse:
                    self._json(board.refuse[0], {"error": board.refuse[1]})
                    return
                self._json(
                    200,
                    {"meeting_id": parts["meeting_id"].decode(), "bytes": len(parts["file"]), "status": "accepted", "ahead": 0, "wait_s": 0},
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        self.uploader = ThreadingHTTPServer(("127.0.0.1", 0), Uploader)
        for httpd in (self.server, self.uploader):
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            test.addCleanup(httpd.server_close)
            test.addCleanup(httpd.shutdown)

    def args(self, *more):
        return [
            *more,
            "--host",
            "127.0.0.1",
            "--server-port",
            str(self.server.server_address[1]),
            "--uploader-port",
            str(self.uploader.server_address[1]),
            "--poll-s",
            "0",
        ]


def audio_file(test, size=300_000):
    fd, path = tempfile.mkstemp(suffix=".ogg")
    with os.fdopen(fd, "wb") as f:
        f.write(bytes(range(256)) * (size // 256))
    test.addCleanup(os.remove, path)
    return path


def run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = upload_meeting.main(argv)
    return code, out.getvalue()


class Upload(unittest.TestCase):
    def test_it_starts_the_worker_and_sends_the_file_as_the_app_does(self):
        board = FakeBoard(self)
        audio = audio_file(self)
        code, out = run(board.args(audio, "--speakers", "15", "--title", "80 分鐘", "--id", "pc-1"))
        self.assertEqual(code, 0, out)
        self.assertEqual(board.started[0]["channel_name"], "meeting-room")
        self.assertEqual(board.started[0]["graph_name"], "meeting_minutes")
        (parts,) = board.uploads
        with open(audio, "rb") as f:
            self.assertEqual(parts["file"], f.read())
        self.assertEqual(
            {k: parts[k].decode() for k in ("meeting_id", "speakers", "title", "script")},
            {"meeting_id": "pc-1", "speakers": "15", "title": "80 分鐘", "script": "traditional"},
        )
        self.assertIn("accepted", out)
        self.assertRegex(out, r"sent \d+(\.\d)? MB in [\d.]+ s, [\d.]+ KB/s")

    def test_a_worker_already_running_is_pinged_not_started_twice(self):
        board = FakeBoard(self, running=True)
        code, out = run(board.args(audio_file(self), "--id", "pc-2"))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(board.pinged), 1)
        self.assertEqual(len(board.uploads), 1)

    def test_follow_waits_for_the_end(self):
        board = FakeBoard(self)
        results = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, results)
        # --out, so a test never writes into the real results folder.
        code, out = run(board.args(audio_file(self), "--id", "pc-3", "--follow", "--out", results))
        self.assertEqual(code, 0, out)
        self.assertIn("transcribing", out)
        self.assertIn("archived", out)

    def test_a_refusal_says_why_and_fails(self):
        board = FakeBoard(self, refuse=(415, "OGG/VORBIS; only Ogg-Opus is accepted"))
        code, out = run(board.args(audio_file(self), "--id", "pc-4"))
        self.assertEqual(code, 1)
        self.assertIn("415", out)
        self.assertIn("only Ogg-Opus", out)


class Results(unittest.TestCase):
    def test_follow_brings_the_minutes_and_the_record_back(self):
        board = FakeBoard(self)
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)
        code, said = run(board.args(audio_file(self), "--id", "pc-5", "--follow", "--out", out))
        self.assertEqual(code, 0, said)
        with open(os.path.join(out, "pc-5", "minutes.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "minutes.txt of pc-5")
        with open(os.path.join(out, "pc-5", "record.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["meeting_id"], "pc-5")
        # And the page to read it in: minutes_html.py, beside them.
        with open(os.path.join(out, "pc-5", "minutes.html"), encoding="utf-8") as f:
            self.assertIn("週會 pc-5", f.read())
        self.assertIn(os.path.join(out, "pc-5", "minutes.html"), said)

    def test_fetch_brings_an_earlier_meetings_results_back(self):
        board = FakeBoard(self, final_after=1)
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)
        code, said = run(board.args("--fetch", "pc-6", "--out", out))
        self.assertEqual(code, 0, said)
        self.assertEqual(board.uploads, [])
        self.assertEqual(len(board.started), 1)
        self.assertTrue(os.path.isfile(os.path.join(out, "pc-6", "minutes.txt")))

    def test_fetch_of_a_meeting_still_processed_says_so(self):
        board = FakeBoard(self, final_after=99)
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)
        code, said = run(board.args("--fetch", "pc-7", "--out", out))
        self.assertEqual(code, 1)
        self.assertIn("transcribing", said)
        self.assertFalse(os.path.exists(os.path.join(out, "pc-7")))

    def test_neither_a_recording_nor_fetch_is_an_error(self):
        board = FakeBoard(self)
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                upload_meeting.main(board.args())


if __name__ == "__main__":
    unittest.main()
