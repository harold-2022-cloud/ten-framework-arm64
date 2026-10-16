#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""set_desktop_autologin.sh on a faked board: SDDM logs the desktop user in
at power-on, into the desktop that user last chose (or the one named, or the
only one there is), through one file of its own that --off removes; and it
says so, rather than writes a file that would not take, when SDDM is not
the login screen or /etc/sddm.conf names someone else.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import getpass
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import AS_ROOT, Sandbox, read  # noqa: E402  pylint: disable=wrong-import-position

USER = "nobody" if AS_ROOT else getpass.getuser()
OURS = "sddm.conf.d/zz-autologin.conf"


class Board(Sandbox):
    def __init__(self, test, sessions=("plasma", "plasmax11"), last=None, main=None, sddm=True):
        super().__init__(test, scripts=["set_desktop_autologin.sh"])
        self.write("sddm.conf.d/.keep", "")
        for name in sessions:
            folder = "xsessions" if name.endswith("x11") else "wayland-sessions"
            self.write(f"{folder}/{name}.desktop", f"[Desktop Entry]\nName={name}\n")
        if last:
            folder = "xsessions" if last.endswith("x11") else "wayland-sessions"
            self.write("state.conf", f"[Last]\nSession={self.path(folder, last + '.desktop')}\nUser={USER}\n")
        if main is not None:
            self.write("sddm.conf", main)
        self.env.update(
            SDDM_CONF_D=self.path("sddm.conf.d"),
            SDDM_CONF=self.path("sddm.conf"),
            SDDM_STATE=self.path("state.conf"),
            SESSION_DIRS=f"{self.path('wayland-sessions')} {self.path('xsessions')}",
            FAKE_ACTIVE="sddm" if sddm else "",
        )

    def autologin(self, *args):
        return self.run(["bash", self.path("set_desktop_autologin.sh"), *args])

    def ours(self):
        path = self.path(OURS)
        return read(path) if os.path.exists(path) else None


class On(unittest.TestCase):
    def test_into_the_desktop_last_chosen(self):
        b = Board(self, last="plasmax11")
        r = b.autologin()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[Autologin]\n", b.ours())
        self.assertIn(f"User={USER}\n", b.ours())
        self.assertIn("Session=plasmax11.desktop\n", b.ours())

    def test_into_the_desktop_named(self):
        b = Board(self, last="plasmax11")
        r = b.autologin("--session", "plasma")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Session=plasma.desktop\n", b.ours())

    def test_the_only_desktop_needs_no_choice(self):
        b = Board(self, sessions=("plasma",))
        r = b.autologin()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Session=plasma.desktop\n", b.ours())


class Refused(unittest.TestCase):
    def test_a_desktop_not_there_is_refused_with_the_list(self):
        b = Board(self)
        r = b.autologin("--session", "gnome")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("plasma, plasmax11", r.stdout)
        self.assertIsNone(b.ours())

    def test_several_desktops_and_none_chosen_before_asks_which(self):
        b = Board(self)
        r = b.autologin()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("--session", r.stdout)
        self.assertIsNone(b.ours())

    def test_sddm_conf_naming_someone_else_is_said(self):
        b = Board(self, last="plasma", main="[Autologin]\nUser=someone\n")
        r = b.autologin()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("sddm.conf", r.stdout)
        self.assertIsNone(b.ours())

    def test_a_login_screen_other_than_sddm_is_refused(self):
        b = Board(self, last="plasma", sddm=False)
        r = b.autologin()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("SDDM", r.stdout)
        self.assertIsNone(b.ours())


class OffAndStatus(unittest.TestCase):
    def test_status_says_who_then_off_removes_it(self):
        b = Board(self, last="plasma")
        self.assertIn("off", b.autologin("--status").stdout)
        b.autologin()
        self.assertIn(f"{USER} into plasma", b.autologin("--status").stdout)
        r = b.autologin("--off")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(b.ours())
        self.assertIn("off", b.autologin("--status").stdout)


if __name__ == "__main__":
    unittest.main()
