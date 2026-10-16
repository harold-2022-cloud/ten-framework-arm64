#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""meeting_screen_kiosk.sh on a faked desktop: it waits for the screen's
page before any browser; keeps the display awake as the desktop allows
(KDE: kde-inhibit around the browser; X11: xset); opens firefox full
screen in a profile of its own that asks nothing, else chromium; and says
so when there is no browser at all.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import os
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Sandbox, free_port, http_server, read, wait_for  # noqa: E402  pylint: disable=wrong-import-position

BROWSERS = ("firefox", "chromium", "chromium-browser", "google-chrome")
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
HAS_BROWSER = any(shutil.which(b, path=SYSTEM_PATH) for b in BROWSERS)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from probe_demo_screen import FIREFOX_PREFS as PROBED_PREFS  # noqa: E402  pylint: disable=wrong-import-position


class Board(Sandbox):
    def __init__(self, test, **env):
        super().__init__(test, scripts=["meeting_screen_kiosk.sh"])
        self.port = free_port()
        self.env.update(
            SCREEN_URL=f"http://127.0.0.1:{self.port}/",
            KIOSK_LOG=self.path("kiosk.log"),
            KIOSK_PROFILE=self.path("home", ".cache", "meeting-screen-browser"),
        )
        self.env.update(env)

    def kiosk(self):
        return self.run(["bash", self.path("meeting_screen_kiosk.sh")], timeout=60)

    def browser_calls(self):
        return [c for c in self.calls() if c.split()[0] in BROWSERS]

    def log(self):
        path = self.path("kiosk.log")
        return read(path) if os.path.exists(path) else ""


class Kiosk(unittest.TestCase):
    def test_it_waits_for_the_page_then_opens_firefox_kept_awake_on_kde(self):
        b = Board(self, XDG_CURRENT_DESKTOP="KDE", XDG_SESSION_TYPE="wayland")
        b.start(["bash", b.path("meeting_screen_kiosk.sh")])
        time.sleep(3)
        self.assertEqual(b.browser_calls(), [])
        http_server(self, b.port)
        self.assertTrue(wait_for(lambda: b.browser_calls(), timeout=20))
        self.assertIn("kde-inhibit --power --screenSaver", b.calls())
        profile, url = b.env["KIOSK_PROFILE"], b.env["SCREEN_URL"]
        self.assertEqual(b.browser_calls(), [f"firefox --kiosk --no-remote --profile {profile} {url}"])
        prefs = read(os.path.join(profile, "user.js"))
        # At least what probe_demo_screen.py showed keeps a fresh profile
        # from opening anything over the page on the board.
        for pref in PROBED_PREFS:
            self.assertIn(f'user_pref("{pref}",', prefs)
        self.assertIn("kde-inhibit", b.log())

    def test_a_kde_inhibit_that_cannot_run_a_command_is_left_out(self):
        b = Board(self, XDG_CURRENT_DESKTOP="KDE", XDG_SESSION_TYPE="wayland")
        b.write("bin/kde-inhibit", "#!/bin/sh\necho \"kde-inhibit $*\" >> \"$FAKE_LOG\"\nexit 1\n", 0o755)
        http_server(self, b.port)
        r = b.kiosk()
        self.assertEqual(r.returncode, 0, b.log())
        self.assertEqual(len(b.browser_calls()), 1)
        self.assertIn("display may dim", b.log())

    def test_on_x11_xset_keeps_the_display_awake(self):
        b = Board(self, XDG_CURRENT_DESKTOP="XFCE", XDG_SESSION_TYPE="x11")
        http_server(self, b.port)
        r = b.kiosk()
        self.assertEqual(r.returncode, 0, b.log())
        self.assertIn("xset s off -dpms", b.calls())
        self.assertFalse([c for c in b.calls() if c.startswith("kde-inhibit")])
        self.assertEqual(len(b.browser_calls()), 1)

    @unittest.skipIf(HAS_BROWSER, "a browser is installed on this machine")
    def test_chromium_when_there_is_no_firefox(self):
        b = Board(self)
        os.replace(os.path.join(b.bin, "firefox"), os.path.join(b.bin, "chromium"))
        http_server(self, b.port)
        r = b.kiosk()
        self.assertEqual(r.returncode, 0, b.log())
        profile, url = b.env["KIOSK_PROFILE"], b.env["SCREEN_URL"]
        self.assertEqual(
            b.browser_calls(),
            [f"chromium --kiosk --noerrdialogs --disable-session-crashed-bubble --no-first-run --user-data-dir={profile} {url}"],
        )

    @unittest.skipIf(HAS_BROWSER, "a browser is installed on this machine")
    def test_no_browser_says_so_and_fails(self):
        b = Board(self)
        os.remove(os.path.join(b.bin, "firefox"))
        http_server(self, b.port)
        r = b.kiosk()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no browser found", b.log())


if __name__ == "__main__":
    unittest.main()
