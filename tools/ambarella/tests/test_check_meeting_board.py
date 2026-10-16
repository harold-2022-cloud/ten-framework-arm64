#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""check_meeting_board.py's preflight asks the LLM the graph itself uses,
and the run finds vLLM's memory -- or says it found none, never 0 MB.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import http.server
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_meeting_board as cmb  # noqa: E402  pylint: disable=wrong-import-position


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Models(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *_):
        pass

    def do_GET(self):  # pylint: disable=invalid-name
        Models.seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(200 if self.path == "/v1/models" else 404)
        self.end_headers()
        self.wfile.write(b'{"data": []}')


class Llm(unittest.TestCase):
    def test_env_references_resolve_as_the_runtime_resolves_them(self):
        ref = "${env:QWEN_LLM_BASE_URL|http://127.0.0.1:8000/v1}"
        os.environ.pop("QWEN_LLM_BASE_URL", None)
        self.assertEqual(cmb.env_ref(ref, {}), "http://127.0.0.1:8000/v1")
        self.assertEqual(cmb.env_ref(ref, {"QWEN_LLM_BASE_URL": "http://x:1/v1"}), "http://x:1/v1")
        self.assertEqual(cmb.env_ref("plain", {}), "plain")

    def test_the_meeting_graph_uses_openai_llm2_python(self):
        addon, prop = cmb.graph_llm("meeting_minutes")
        self.assertEqual(addon, "openai_llm2_python")
        self.assertEqual(prop["model"], "qwen3.5_4B")

    def test_nothing_answering_fails_and_says_how_to_start_it(self):
        prop = {"base_url": f"http://127.0.0.1:{free_port()}/v1", "api_key": "EMPTY", "model": "qwen3.5_4B"}
        good, _, fix = cmb.check_llm("openai_llm2_python", prop, {})
        self.assertFalse(good)
        self.assertIn("serve_qwen35.sh start", fix)

    def test_models_answering_passes_with_the_key(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Models)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        prop = {"base_url": f"http://127.0.0.1:{server.server_address[1]}/v1", "api_key": "EMPTY", "model": "qwen3.5_4B"}
        good, message, _ = cmb.check_llm("openai_llm2_python", prop, {})
        self.assertTrue(good, message)
        self.assertIn(("/v1/models", "Bearer EMPTY"), Models.seen)


class Memory(unittest.TestCase):
    def test_a_vllm_serve_process_is_found(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "vllm", "serve"])
        self.addCleanup(proc.kill)
        time.sleep(0.5)
        mb = cmb.vllm_rss_mb()
        self.assertIsNotNone(mb)
        self.assertGreater(mb, 1)

    def test_no_such_process_is_none_not_zero(self):
        self.assertIsNone(cmb.vllm_rss_mb(program="no-such-program-q42", pid_file="/nonexistent/pid"))

    def test_a_vllm_under_another_title_is_found_by_its_pid_file(self):
        # vLLM may rename its processes; serve_qwen35.sh's pid file still
        # names the one it started.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.kill)
        time.sleep(0.5)
        with tempfile.NamedTemporaryFile("w", suffix=".pid", delete=False) as f:
            f.write(f"{proc.pid}\n")
        self.addCleanup(os.unlink, f.name)
        mb = cmb.vllm_rss_mb(program="no-such-program-q46", pid_file=f.name)
        self.assertIsNotNone(mb)
        self.assertGreater(mb, 1)

    def test_a_pid_file_of_a_process_gone_is_none(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        with tempfile.NamedTemporaryFile("w", suffix=".pid", delete=False) as f:
            f.write(f"{proc.pid}\n")
        self.addCleanup(os.unlink, f.name)
        self.assertIsNone(cmb.vllm_rss_mb(program="no-such-program-q46", pid_file=f.name))

    def test_memory_available_is_read(self):
        self.assertGreater(cmb.mem_available_mb(), 0)


class Timing(unittest.TestCase):
    def test_each_topic_says_where_its_time_went(self):
        lines = cmb.timing_lines(
            [
                {
                    "id": "t01",
                    "start_s": 0.0,
                    "end_s": 360.0,
                    "timing": {
                        "diarize_s": 170.0,
                        "recognise_s": 120.0,
                        "embed_s": 30.0,
                        "turns": 85,
                    },
                },
                {"id": "t02", "start_s": 360.0, "end_s": 420.0},
            ]
        )
        self.assertEqual(
            lines,
            [
                "t01 (06:00): told speakers apart in 170 s (0.47 x), "
                "recognised in 120 s (0.33 x), voices embedded in 30 s, 85 turns"
            ],
        )

    def test_a_record_without_timing_says_nothing(self):
        self.assertEqual(cmb.timing_lines([{"id": "t01"}]), [])


if __name__ == "__main__":
    unittest.main()
