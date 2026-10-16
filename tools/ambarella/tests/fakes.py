#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A board, faked, for the tests of tools/ambarella's shell scripts.

Sandbox: a folder with a home, the fakes of tests/fakes/ ahead on PATH,
copies of the scripts under test, and settings that keep everything off the
machine's own ports. Scripts that refuse root run as nobody when the tests
run as root -- on files nobody owns, as the board runs them as lychee on
lychee's -- and as the caller otherwise.
"""

import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TESTS = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(TESTS)
FAKES = os.path.join(TESTS, "fakes")
AS_ROOT = os.geteuid() == 0


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(check, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.2)
    return False


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def answers(url, timeout=1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


class _Any(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):  # pylint: disable=invalid-name
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")


def http_server(test, port):
    """Something that answers at port until the test ends."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Any)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return server


def gone(pid):
    """Not running: no such process, or one waiting to be reaped."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().split(")")[-1].split()[0] == "Z"
    except OSError:
        return True


class Sandbox:
    def __init__(self, test, scripts=(), board_user=True):
        self.root = tempfile.mkdtemp(prefix="qtools-")
        self.board_user = board_user and AS_ROOT
        test.addCleanup(self.close)
        self.home = self.path("home")
        self.bin = self.path("bin")
        os.makedirs(self.home)
        shutil.copytree(FAKES, self.bin)
        # The 7B daemon's stand-in: a python that pgrep -x knows as test_llm.
        os.symlink(os.path.realpath(sys.executable), os.path.join(self.bin, "test_llm"))
        for script in scripts:
            shutil.copy(os.path.join(TOOLS, script), self.root)
        llm_dir = self.path("llm_demo")
        os.makedirs(llm_dir)
        shutil.move(os.path.join(self.bin, "run_llm_demo.sh"), llm_dir)
        self.env = {
            "PATH": f"{self.bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": self.home,
            "LANG": "C.UTF-8",
            "FAKE_DIR": self.root,
            "FAKE_LOG": self.path("calls.log"),
            "QWEN_PORT": str(free_port()),
            "QWEN_WAIT_S": "60",
            "LLM_PORT": str(free_port()),
            "LLM_DIR": llm_dir,
            "DAEMON_LOG": self.path("log.txt"),
            "GIVE_BACK_S": "30",
            "MEETING_UPLOADER": f"http://127.0.0.1:{free_port()}",
            # Where the scripts keep their logs: here, not the machine's /tmp.
            "LOG_DIR": self.root,
        }
        self.own()

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def own(self):
        """Everything here the board user's."""
        if not self.board_user:
            return
        nobody = pwd.getpwnam("nobody")
        for folder, dirs, files in os.walk(self.root):
            for name in [folder] + [os.path.join(folder, n) for n in dirs + files]:
                os.lchown(name, nobody.pw_uid, nobody.pw_gid)

    def write(self, rel, text, mode=0o644):
        path = self.path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, mode)
        self.own()
        return path

    def install_qwen(self):
        """Qwen3.5-4B installed where serve_qwen35.sh looks for it."""
        self.write("home/workspace/cooper-vllm/venv_vllm/bin/vllm", read(os.path.join(FAKES, "vllm")), 0o755)
        self.write("home/demo_resources/llm_demo/qwen3.5_4B/ranch/config.json", "{}")

    def argv(self, argv, env):
        full = dict(self.env, **{k: str(v) for k, v in env.items()})
        prefix = ["runuser", "-u", "nobody", "--"] if self.board_user else []
        return prefix + ["env", "-i"] + [f"{k}={v}" for k, v in full.items()] + list(argv)

    def run(self, argv, timeout=240, **env):
        return subprocess.run(
            self.argv(argv, env), cwd=self.root, capture_output=True, text=True,
            timeout=timeout, check=False,
        )

    def start(self, argv, **env):
        """In the background, as the leader of a process group of its own, as
        a terminal's foreground job would be."""
        return subprocess.Popen(
            self.argv(argv, env), cwd=self.root, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )

    def start_7b(self):
        """The 7B daemon's stand-in, as if started by hand."""
        self.start([os.path.join(self.bin, "test_llm"), "-m", "http.server",
                    self.env["LLM_PORT"], "--bind", "127.0.0.1"])
        return wait_for(lambda: answers(f"http://127.0.0.1:{self.env['LLM_PORT']}/"))

    def calls(self):
        try:
            return read(self.env["FAKE_LOG"]).splitlines()
        except OSError:
            return []

    def close(self):
        # Whatever still runs from here: the fakes' services outlive the
        # calls that started them.
        subprocess.run(["pkill", "-f", self.root], check=False)
        time.sleep(0.5)
        subprocess.run(["pkill", "-9", "-f", self.root], check=False)
        shutil.rmtree(self.root, ignore_errors=True)
