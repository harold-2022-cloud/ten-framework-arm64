#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""serve_qwen35.sh on a faked board: vLLM's command line in one place, with
the tool flags; the tool-call test; the service's exec and ready; start,
stop and restart through systemctl once the services are installed; which
and back, which put the board back as a script found it.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Sandbox, gone, http_server, read, wait_for  # noqa: E402  pylint: disable=wrong-import-position

SERVICES = "qwen35-vllm ambarella-llm"
RUN = "home/workspace/.qwen35-serve"


class Board(Sandbox):
    def __init__(self, test, **env):
        super().__init__(test, scripts=["serve_qwen35.sh"])
        self.test = test
        self.install_qwen()
        self.env["FAKE_SERVE"] = self.path("serve_qwen35.sh")
        self.env.update(env)

    def serve(self, *args):
        return self.run(["bash", self.path("serve_qwen35.sh"), *args])

    def ok(self, *args):
        r = self.serve(*args)
        self.test.assertEqual(
            r.returncode, 0, f"serve_qwen35.sh {' '.join(args)}:\n{r.stdout}{r.stderr}"
        )
        return r

    def which(self):
        return self.serve("which").stdout.strip()

    def pid(self):
        return read(self.path(RUN, "pid")).strip()


def with_the_services(test):
    """The services installed and the 7B serving through its own, as the
    board is before the switch."""
    board = Board(test, FAKE_UNITS=SERVICES)
    r = board.run(["systemctl", "start", "ambarella-llm"])
    test.assertEqual(r.returncode, 0, r.stderr)
    test.assertTrue(wait_for(lambda: board.which() == "7b"))
    return board


class CommandLine(unittest.TestCase):
    def test_vllm_runs_with_the_tool_flags(self):
        b = Board(self)
        b.ok("start")
        argv = read(b.path("vllm.argv")).splitlines()
        self.assertIn("--enable-auto-tool-choice", argv)
        self.assertEqual(argv[argv.index("--tool-call-parser") + 1], "hermes")
        self.assertEqual(argv[argv.index("--reasoning-parser") + 1], "qwen3")

    def test_vllm_keeps_no_prompts_in_its_log(self):
        # vLLM logs every request whole by default: every meeting's
        # transcript, kept in vllm_qwen35.log for good.
        b = Board(self)
        b.ok("start")
        self.assertIn("--disable-log-requests", read(b.path("vllm.argv")).splitlines())

    def test_a_pid_file_naming_another_process_is_left_alone(self):
        # A pid file outliving its vLLM, the number since taken by another
        # process of the user's -- a login shell, say, with a session of its own.
        b = Board(self)
        other = b.path("other.pid")
        b.start([sys.executable, "-c",
                 "import os, time\n"
                 "try:\n    os.setsid()\nexcept OSError:\n    pass\n"
                 f"open({other!r}, 'w').write(str(os.getpid()))\n"
                 "time.sleep(120)\n"])
        self.assertTrue(wait_for(lambda: os.path.exists(other) and read(other)))
        pid = read(other)
        b.write(f"{RUN}/pid", pid + "\n")
        b.write(f"{RUN}/pid.start", "1\n")
        self.assertIn("Qwen3.5-4B not running", b.ok("status").stdout)
        b.ok("stop")
        self.assertFalse(gone(pid))

    def test_the_arguments_it_runs_on_are_kept_and_not_stale(self):
        b = Board(self)
        b.ok("start")
        self.assertEqual(read(b.path(RUN, "args")), read(b.path("vllm.argv")))
        self.assertNotEqual(b.serve("stale").returncode, 0)

    def test_older_arguments_are_stale_and_status_says_restart(self):
        b = Board(self)
        b.ok("start")
        b.write(f"{RUN}/args", "serve\n--an-older-flag\n")
        self.assertEqual(b.serve("stale").returncode, 0)
        self.assertIn("serve_qwen35.sh restart", b.ok("status").stdout)


class ToolCalls(unittest.TestCase):
    def test_a_streamed_tool_call_passes(self):
        b = Board(self)
        b.ok("start")
        self.assertIn("get_future_weather", b.ok("test-tools").stdout)

    def test_vllm_without_the_tool_flags_says_restart(self):
        b = Board(self, FAKE_NO_TOOL_FLAGS="1")
        b.ok("start")
        r = b.serve("test-tools")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("serve_qwen35.sh restart", r.stdout)

    def test_a_tool_call_written_as_text_fails(self):
        b = Board(self, FAKE_TOOL_AS_TEXT="1")
        b.ok("start")
        r = b.serve("test-tools")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("as text", r.stdout)


class Service(unittest.TestCase):
    def test_exec_becomes_vllm_and_ready_waits_for_it(self):
        b = Board(self, FAKE_LOAD_S="4")
        b.start(["bash", b.path("serve_qwen35.sh"), "exec"])
        t0 = time.monotonic()
        b.ok("ready", "60")
        self.assertGreaterEqual(time.monotonic() - t0, 3)
        # The pid exec wrote is vLLM's own: nothing forked in between.
        with open(f"/proc/{b.pid()}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
        self.assertTrue(any(a.endswith(b"/venv_vllm/bin/vllm") for a in argv), argv)
        self.assertIn("serve_qwen35.sh exec", read(b.path("home/workspace/vllm_qwen35.log")))

    def test_ready_fails_when_vllm_exits_first(self):
        b = Board(self, FAKE_EXIT="1")
        b.start(["bash", b.path("serve_qwen35.sh"), "exec"])
        t0 = time.monotonic()
        r = b.serve("ready", "120")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("exited before it answered", r.stdout)
        self.assertLess(time.monotonic() - t0, 30)


class WithTheServices(unittest.TestCase):
    def test_start_stops_the_7b_then_starts_qwen35_vllm(self):
        b = with_the_services(self)
        b.ok("start")
        self.assertEqual(b.which(), "qwen")
        calls = b.calls()
        self.assertLess(
            calls.index("systemctl stop ambarella-llm"),
            calls.index("systemctl start qwen35-vllm"),
        )
        self.assertEqual(read(b.path("units", "qwen35-vllm.pid")).strip(), b.pid())

    def test_stop_brings_the_7b_up(self):
        b = with_the_services(self)
        b.ok("start")
        r = b.ok("stop")
        self.assertEqual(b.which(), "7b")
        self.assertIn("systemctl stop qwen35-vllm", b.calls())
        self.assertIn("the DeepSeek 7B daemon is back", r.stdout)

    def test_restart_goes_through_qwen35_vllm(self):
        b = with_the_services(self)
        b.ok("start")
        before = b.pid()
        b.ok("restart")
        self.assertIn("systemctl restart qwen35-vllm", b.calls())
        self.assertNotEqual(b.pid(), before)
        self.assertEqual(b.which(), "qwen")

    def test_a_vllm_started_by_hand_gives_way_to_the_service(self):
        b = Board(self)
        b.ok("start")
        by_hand = b.pid()
        b.env["FAKE_UNITS"] = SERVICES
        b.ok("restart")
        self.assertTrue(gone(by_hand))
        self.assertEqual(read(b.path("units", "qwen35-vllm.pid")).strip(), b.pid())

    def test_restart_refuses_while_a_meeting_worker_is_up(self):
        b = with_the_services(self)
        b.ok("start")
        http_server(self, int(b.env["MEETING_UPLOADER"].rsplit(":", 1)[1]))
        r = b.serve("restart")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("a meeting worker is up", r.stdout)
        self.assertEqual(b.which(), "qwen")


class MeetingGuard(unittest.TestCase):
    """Every graph asks Qwen3.5-4B: taking it away while a meeting is being
    processed must wait; bringing it back must not."""

    def test_stop_refuses_while_a_meeting_worker_is_up(self):
        b = with_the_services(self)
        b.ok("start")
        http_server(self, int(b.env["MEETING_UPLOADER"].rsplit(":", 1)[1]))
        r = b.serve("stop")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("a meeting worker is up", r.stdout)
        self.assertEqual(b.which(), "qwen")

    def test_back_to_qwen_goes_ahead_while_a_meeting_worker_is_up(self):
        b = with_the_services(self)
        http_server(self, int(b.env["MEETING_UPLOADER"].rsplit(":", 1)[1]))
        b.ok("back", "qwen")
        self.assertEqual(b.which(), "qwen")


class Back(unittest.TestCase):
    def test_after_a_switch_from_the_7b_back_brings_the_7b(self):
        b = with_the_services(self)
        was = b.which()
        b.ok("start")
        b.ok("back", was)
        self.assertEqual(b.which(), "7b")

    def test_back_to_qwen_when_qwen_serves_switches_nothing(self):
        b = with_the_services(self)
        b.ok("start")
        before = len(b.calls())
        b.ok("back", "qwen")
        switched = [c for c in b.calls()[before:] if c.split()[1:2] in (["start"], ["stop"], ["restart"])]
        self.assertEqual(switched, [])

    def test_back_to_none_leaves_qwen_serving(self):
        b = with_the_services(self)
        b.ok("start")
        b.ok("back", "none")
        self.assertEqual(b.which(), "qwen")

    def test_back_wants_what_which_said(self):
        self.assertEqual(Board(self).serve("back", "deepseek").returncode, 1)


if __name__ == "__main__":
    unittest.main()
