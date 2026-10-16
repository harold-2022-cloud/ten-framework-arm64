#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""install_board_services.sh on a faked board: the qwen35-vllm unit,
ten-api ordered after it, ambarella-llm installed but not started at boot;
an install switches the board to Qwen3.5-4B -- not while a meeting worker
is up -- and restarts a vLLM on older arguments; --status says when ten-api
started against when Qwen3.5-4B was up.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import getpass
import glob
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import AS_ROOT, Sandbox, free_port, http_server, read  # noqa: E402  pylint: disable=wrong-import-position

USER = "nobody" if AS_ROOT else getpass.getuser()


class Board(Sandbox):
    def __init__(self, test, which="7b", stale=False, worker=False, active=""):
        super().__init__(test, scripts=["install_board_services.sh"])
        self.test = test
        self.api_port = api = free_port()
        http_server(test, api)
        if worker:
            http_server(test, int(self.env["MEETING_UPLOADER"].rsplit(":", 1)[1]))
        self.write("server/bin/api", "#!/bin/sh\n", 0o755)
        self.write(".env", f"SERVER_PORT={api}\n")
        self.write("model/.keep", "")
        self.write("vllm", "#!/bin/sh\n", 0o755)
        self.write("systemd/.keep", "")
        self.screen_port = free_port()
        http_server(test, self.screen_port)
        self.write("meeting-screen/meeting_screen/server.py", "")
        self.write("kiosk.sh", "#!/bin/sh\n", 0o755)
        self.env.update(
            SERVER_DIR=self.path("server"),
            ENV_FILE=self.path(".env"),
            SERVE=self.path("bin", "fake_serve"),
            UNIT_DIR=self.path("systemd"),
            ENV_OUT=self.path("ten-api.env"),
            TASK=self.path("bin", "task"),
            LLM_MODEL_PATH=self.path("model"),
            QWEN_VLLM=self.path("vllm"),
            TASK_LOG=self.path("task_run.log"),
            FAKE_WHICH=which,
            FAKE_UNITS="qwen35-vllm ambarella-llm ten-api",
            FAKE_ACTIVE=active,
            SCREEN_DIR=self.path("meeting-screen"),
            SCREEN_PY=sys.executable,
            SCREEN_PORT=str(self.screen_port),
            SCREEN_LOG=self.path("meeting_screen.log"),
            KIOSK_SCRIPT=self.path("kiosk.sh"),
            AUTOSTART=self.path("autostart", "meeting-screen.desktop"),
        )
        if stale:
            self.env["FAKE_STALE"] = "1"

    def install(self, *args):
        return self.run(["bash", self.path("install_board_services.sh"), *args])

    def printed(self):
        """--print, as {unit file name: its text}."""
        r = self.install("--print")
        self.test.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        units, name = {}, None
        for line in r.stdout.splitlines():
            if line.startswith("# ") and line.endswith(".service"):
                name = os.path.basename(line[2:])
                units[name] = ""
            elif name:
                units[name] += line + "\n"
        return units

    def switches(self):
        """What the install asked serve_qwen35.sh to do, its questions aside."""
        return [c for c in self.calls() if c.startswith("serve ") and c not in ("serve which", "serve stale")]


class Units(unittest.TestCase):
    def test_qwen35_vllm_runs_serve_exec_and_waits_for_ready(self):
        b = Board(self)
        unit = b.printed()["qwen35-vllm.service"]
        serve = b.env["SERVE"]
        for line in (
            f"User={USER}",
            f'ExecStart="{serve}" exec',
            f'ExecStartPost="{serve}" ready 300',
            "TimeoutStartSec=330",
            "Restart=always",
            "Conflicts=ambarella-llm.service",
            "After=network-online.target ambarella-llm.service",
            "WantedBy=multi-user.target",
        ):
            self.assertIn(line + "\n", unit)

    def test_ten_api_starts_after_qwen35_vllm(self):
        b = Board(self)
        self.assertIn(
            "After=network-online.target qwen35-vllm.service\n",
            b.printed()["ten-api.service"],
        )

    @unittest.skipUnless(shutil.which("systemd-analyze"), "no systemd-analyze here")
    def test_systemd_reads_the_units(self):
        b = Board(self)
        paths = [b.write(f"check/{name}", text) for name, text in b.printed().items()]
        r = subprocess.run(["systemd-analyze", "verify", *paths], capture_output=True, text=True, check=False)
        # Only what it says of these units: it reads the units they name as
        # well, and this machine's own may carry keys its systemd does not know.
        bad = [
            line for line in r.stderr.splitlines()
            if line.startswith(b.path("check"))
            and any(word in line for word in ("Unknown", "Invalid", "not executable", "bad-setting"))
        ]
        self.assertEqual(bad, [], r.stderr)


class Switch(unittest.TestCase):
    def test_an_idle_board_is_switched_to_qwen(self):
        b = Board(self, which="7b")
        r = b.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(b.switches(), ["serve start"])
        calls = b.calls()
        self.assertIn("systemctl enable qwen35-vllm ten-api meeting-screen", calls)
        self.assertIn("systemctl disable ambarella-llm", calls)
        self.assertEqual(
            sorted(f for f in os.listdir(b.path("systemd")) if f.endswith(".service")),
            ["ambarella-llm.service", "meeting-screen.service", "qwen35-vllm.service", "ten-api.service"],
        )

    def test_a_meeting_in_progress_keeps_its_llm(self):
        b = Board(self, which="7b", worker=True)
        r = b.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(b.switches(), [])
        self.assertIn("a meeting worker is up", r.stdout)
        self.assertIn("systemctl enable qwen35-vllm ten-api meeting-screen", b.calls())

    def test_a_vllm_on_older_arguments_is_restarted(self):
        b = Board(self, which="qwen", stale=True, active="qwen35-vllm")
        r = b.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(b.switches(), ["serve restart"])

    def test_a_qwen_started_by_hand_gives_way_to_the_service(self):
        b = Board(self, which="qwen")
        r = b.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(b.switches(), ["serve restart"])

    def test_qwen_on_its_service_and_arguments_is_left_alone(self):
        b = Board(self, which="qwen", active="qwen35-vllm")
        r = b.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(b.switches(), [])


class Screen(unittest.TestCase):
    def test_the_screen_unit_reads_meetings_dir_and_showcase_from_env_file(self):
        b = Board(self)
        b.write(".env", f"SERVER_PORT={b.api_port}\nMEETINGS_DIR=/data/meetings\nMEETING_SCREEN_SHOWCASE=demo-1\n")
        unit = b.printed()["meeting-screen.service"]
        self.assertIn(f"User={USER}\n", unit)
        self.assertIn(f"WorkingDirectory={b.path('meeting-screen')}\n", unit)
        self.assertIn(
            f"ExecStart=/bin/sh -c \"exec '{sys.executable}' -m meeting_screen --meetings-dir '/data/meetings'"
            f" --listen 127.0.0.1:{b.screen_port} --showcase 'demo-1' >> '{b.path('meeting_screen.log')}' 2>&1\"\n",
            unit,
        )
        self.assertIn("Restart=on-failure\n", unit)

    def test_meetings_dir_defaults_as_the_graph_does(self):
        b = Board(self)
        unit = b.printed()["meeting-screen.service"]
        self.assertIn("--meetings-dir '/tmp/meetings'", unit)
        self.assertNotIn("--showcase", unit)

    def test_install_starts_the_screen_and_checks_it_answers(self):
        b = Board(self, which="qwen", active="qwen35-vllm")
        r = b.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("systemctl restart meeting-screen", b.calls())
        self.assertIn("demo screen answers at", r.stdout)

    def test_kiosk_on_and_off_without_sudo(self):
        b = Board(self)
        r = b.install("--kiosk", "on")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        entry = read(b.path("autostart", "meeting-screen.desktop"))
        self.assertIn(f"Exec={b.path('kiosk.sh')}\n", entry)
        self.assertIn("[Desktop Entry]\n", entry)
        self.assertFalse([c for c in b.calls() if c.startswith("systemctl")])
        r = b.install("--kiosk", "off")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(b.path("autostart", "meeting-screen.desktop")))

    def test_kiosk_takes_on_or_off(self):
        self.assertEqual(Board(self).install("--kiosk", "maybe").returncode, 2)

    def test_status_shows_the_screen_and_the_kiosk(self):
        b = Board(self, which="qwen", active="meeting-screen")
        out = b.install("--status").stdout
        self.assertIn("meeting-screen ", out)
        self.assertIn("demo screen answers at", out)
        self.assertIn("kiosk: off", out)
        b.install("--kiosk", "on")
        self.assertIn("kiosk: the screen opens when the desktop starts", b.install("--status").stdout)


class Status(unittest.TestCase):
    def times(self, b, qwen_up, api_start):
        b.write("units/qwen35-vllm.ActiveEnterTimestampMonotonic", f"{qwen_up}\n")
        b.write("units/ten-api.ExecMainStartTimestampMonotonic", f"{api_start}\n")

    def test_ten_api_after_qwen_since_boot(self):
        b = Board(self, which="qwen")
        self.times(b, 62_000_000, 63_500_000)
        r = b.install("--status")
        self.assertIn("since boot: Qwen3.5-4B up at 62 s, ten-api started at 63 s", r.stdout)

    def test_ten_api_before_qwen_is_said(self):
        b = Board(self, which="qwen")
        self.times(b, 62_000_000, 20_000_000)
        self.assertIn("before Qwen3.5-4B was up", b.install("--status").stdout)

    def test_the_7b_serving_is_said(self):
        b = Board(self, which="7b")
        self.assertIn("the DeepSeek 7B daemon serves", b.install("--status").stdout)


class Log(unittest.TestCase):
    def test_an_install_is_kept_in_a_log_of_the_day(self):
        b = Board(self, which="qwen", active="qwen35-vllm")
        r = b.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (log,) = glob.glob(b.path("install_board_services_[0-9]*_[0-9]*.log"))
        self.assertIn("demo screen answers at", read(log))

    def test_print_writes_no_log(self):
        b = Board(self)
        b.printed()
        self.assertEqual(glob.glob(b.path("install_board_services_*.log")), [])


if __name__ == "__main__":
    unittest.main()
