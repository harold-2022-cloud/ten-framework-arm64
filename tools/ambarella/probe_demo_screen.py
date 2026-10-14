#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Check on the board what the meeting demo screen will rely on, before it
is built (docs/superpowers/specs/2026-10-14-meeting-demo-screen-design.md).

  python3 tools/ambarella/probe_demo_screen.py
  python3 tools/ambarella/probe_demo_screen.py --no-kiosk
  python3 tools/ambarella/probe_demo_screen.py --seconds 60
  python3 tools/ambarella/probe_demo_screen.py --url http://127.0.0.1:8765/

Run it as the user logged in on the board's desktop (lychee), not with sudo,
under the system python3 -- the screen service will run under it, standard
library only. Over SSH is fine: the desktop session is found from its
processes.

What it checks, in order:

  user      not root; the system python3
  display   the board's display connectors and their modes (/sys/class/drm)
  session   this user's graphical session: its type, desktop and display
  login     whether the display manager logs this user in at boot
  browser   the browsers installed, and their versions
  fonts     a Traditional Chinese font, and the one sans-serif resolves to
  blanking  whether the screen goes dark when idle, as the desktop is set now
  input     keyboards, mice and presenter remotes plugged in
  service   port 8766 free; where meetings land (not tmpfs, room); the
            board's addresses, for the phone app
  kiosk     opens a test page full screen for --seconds (30): whether it
            fills the screen and is in front, its size and frame rate, and
            what the browser costs in memory and CPU. Skipped while a meeting
            is being processed, since it would take CPU from it.

Nothing on the board is changed: the browser it opens is closed again and its
temporary profile removed. Everything printed is also written to a log, named
at the end. Exits non-zero if any check failed.
"""

import argparse
import glob
import http.server
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ENV_FILE = os.path.join(REPO, "ai_agents", ".env")

SCREEN_PORT = 8766
UPLOADER = "http://127.0.0.1:8765"
FINAL = ("archived", "empty", "failed")
# Peak resident memory of the meeting worker over a whole 38-minute meeting
# (verify_meeting_board.sh, 2026-10-07): what the browser has to fit beside.
MEETING_PEAK_MB = 1898

# In the order the kiosk prefers them.
BROWSERS = (
    "firefox",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
)
FLATPAKS = ("org.mozilla.firefox", "org.chromium.Chromium", "com.google.Chrome")
# Processes that carry a desktop session's environment.
SESSION_PROCS = (
    "gnome-shell",
    "plasmashell",
    "kwin_wayland",
    "kwin_x11",
    "xfce4-session",
    "xfwm4",
    "lxqt-session",
    "mate-session",
    "cinnamon",
    "weston",
    "sway",
    "labwc",
    "gnome-session-b",
)
SESSION_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_TYPE",
)
# A fresh Firefox profile otherwise opens welcome, privacy and translation
# prompts over the page.
FIREFOX_PREFS = {
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.aboutwelcome.enabled": False,
    "trailhead.firstrun.didSeeAboutWelcome": True,
    "datareporting.policy.dataSubmissionEnabled": False,
    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "browser.sessionstore.resume_from_crash": False,
    "browser.translations.automaticallyPopup": False,
    "browser.tabs.warnOnClose": False,
}
CHROMIUM_FLAGS = (
    "--kiosk",
    "--noerrdialogs",
    "--disable-session-crashed-bubble",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--disable-features=Translate",
)
LOAD_TIMEOUT_S = 45

failures = []
warnings = []
log_lines = []


# --------------------------------------------------------------------------
# output, as the other board tools print it


def emit(line):
    print(line, flush=True)
    log_lines.append(line)


def say(msg):
    emit(f"\n== {msg}")


def ok(msg):
    emit(f"  ok    {msg}")


def info(msg):
    emit(f"        {msg}")


def warn(msg, fix=None):
    warnings.append(msg)
    emit(f"  warn  {msg}")
    if fix:
        emit(f"        -> {fix}")


def fail(msg, fix=None):
    failures.append(msg)
    emit(f"  FAIL  {msg}")
    if fix:
        emit(f"        -> {fix}")


# --------------------------------------------------------------------------
# small helpers


def run(cmd, env=None, timeout=15):
    """(exit code, stdout); 127 when the command is not there."""
    try:
        done = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        # Not there, or a PATH entry this user may not enter: either way,
        # not something it can run.
        return 127, ""
    return done.returncode, done.stdout.decode("utf-8", "replace")


def read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def dotenv():
    out = {}
    for line in read(ENV_FILE).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def setting(env, key, default):
    return os.environ.get(key) or env.get(key) or default


def filesystem_of(path):
    path = os.path.realpath(
        path if os.path.exists(path) else os.path.dirname(path)
    )
    best, fs = "", "?"
    for line in read("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount, kind = parts[1], parts[2]
        inside = path == mount or path.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) > len(best):
            best, fs = mount, kind
    return fs


def free_mb(path):
    while not os.path.exists(path):
        path = os.path.dirname(path)
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1024 / 1024


def mem_available_mb():
    m = re.search(r"^MemAvailable:\s+(\d+)", read("/proc/meminfo"), re.M)
    return int(m.group(1)) / 1024 if m else 0.0


def mem_total_mb():
    m = re.search(r"^MemTotal:\s+(\d+)", read("/proc/meminfo"), re.M)
    return int(m.group(1)) / 1024 if m else 0.0


def answers(url, timeout=2):
    """The HTTP status, or 0 when nothing answered."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as err:
        return err.code
    except Exception:  # pylint: disable=broad-except
        return 0


# --------------------------------------------------------------------------
# processes


def proc_stat(pid):
    """(session id, cpu ticks) of a process, or None once it is gone."""
    text = read(f"/proc/{pid}/stat")
    if not text:
        return None
    rest = text[text.rfind(")") + 2 :].split()
    # After the command: state, ppid, pgrp, session, ...; utime is the 12th
    # field, stime the 13th.
    return int(rest[3]), int(rest[11]) + int(rest[12])


def session_members(sid):
    """Every process of this user in session sid. The browser is started
    in a session of its own, so this finds all of it -- even when a
    launcher script forks the browser and exits, which a walk down from
    the launcher's pid would lose."""
    out = []
    for pid in own_processes():
        st = proc_stat(pid)
        if st is not None and st[0] == sid and pid != os.getpid():
            out.append(pid)
    return out


def cpu_ticks(pids):
    return sum((proc_stat(p) or (0, 0))[1] for p in pids)


def memory_mb(pid):
    """Proportional set size: summing it over processes does not count the
    pages they share more than once. VmRSS when the kernel has no rollup."""
    m = re.search(r"^Pss:\s+(\d+)", read(f"/proc/{pid}/smaps_rollup"), re.M)
    if not m:
        m = re.search(r"^VmRSS:\s+(\d+)", read(f"/proc/{pid}/status"), re.M)
    return int(m.group(1)) / 1024 if m else 0.0


def own_processes():
    me = os.getuid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != me:
                continue
        except OSError:
            continue
        yield int(entry)


def environ_of(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    out = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            out[key.decode("utf-8", "replace")] = value.decode(
                "utf-8", "replace"
            )
    return out


# --------------------------------------------------------------------------
# the checks


def check_user():
    say("user")
    if os.geteuid() == 0:
        fail(
            "running as root",
            "run it as the user logged in on the board's desktop (lychee), "
            "without sudo: the browser has to open in that user's session",
        )
        return None
    name = pwd.getpwuid(os.getuid()).pw_name
    ok(f"running as {name}")
    version = sys.version.split()[0]
    if sys.version_info < (3, 8):
        fail(f"python3 is {version}; the screen service needs 3.8 or newer")
    else:
        ok(
            f"python3 {version} ({sys.executable}), what the screen service runs on"
        )
    return name


def check_display():
    say("display")
    connected = []
    for path in sorted(glob.glob("/sys/class/drm/card*-*")):
        name = re.sub(r"^card\d+-", "", os.path.basename(path))
        status = read(os.path.join(path, "status")).strip()
        if not status:
            continue
        modes = read(os.path.join(path, "modes")).split()
        if status == "connected":
            connected.append((name, modes))
            shown = ", ".join(dict.fromkeys(modes[:4])) or "no modes listed"
            ok(f"{name}: connected; modes {shown} (first is preferred)")
        else:
            info(f"{name}: {status}")
    if not connected:
        fail(
            "no display connector reports connected",
            "plug the screen into the board's HDMI, then run this again",
        )
        return None
    modes = connected[0][1]
    if modes and modes[0] != "1920x1080":
        warn(
            f"the screen prefers {modes[0]}, not 1920x1080: the layout is "
            "designed for 1080p and will be scaled"
        )
    return modes[0] if modes else None


def find_session(display_override):
    """The environment a browser needs to open in the desktop session, and
    where it was found."""
    if display_override:
        return {"DISPLAY": display_override}, "--display"
    # (rank, pid, comm, environment); a desktop shell ranks before any
    # other process that merely inherited the session's display.
    best = (2, 0, "", {})
    for pid in own_processes():
        env = environ_of(pid)
        if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
            continue
        if env.get("SSH_CONNECTION"):
            # An X-forwarding SSH login has a DISPLAY too; it is not the
            # board's screen.
            continue
        comm = read(f"/proc/{pid}/comm").strip()
        rank = 0 if comm in SESSION_PROCS else 1
        if rank < best[0]:
            best = (rank, pid, comm, env)
    _, pid, comm, env = best
    if not env:
        return None, None
    found = {k: env[k] for k in SESSION_KEYS if env.get(k)}
    return found, f"{comm} (pid {pid})"


def check_session(name, display_override):
    say("session")
    code, out = run(["loginctl", "list-sessions", "--no-legend"])
    graphical = []
    if code == 0:
        for line in out.splitlines():
            fields = line.split()
            if len(fields) < 3 or fields[2] != name:
                continue
            _, props = run(
                [
                    "loginctl",
                    "show-session",
                    fields[0],
                    "-p",
                    "Type",
                    "-p",
                    "State",
                    "-p",
                    "Remote",
                    "-p",
                    "Desktop",
                ]
            )
            p = dict(kv.split("=", 1) for kv in props.splitlines() if "=" in kv)
            kind = p.get("Type", "?")
            info(
                f"login session {fields[0]}: {kind}, {p.get('State', '?')}"
                + (", remote" if p.get("Remote") == "yes" else "")
                + (f", desktop {p['Desktop']}" if p.get("Desktop") else "")
            )
            if kind in ("x11", "wayland") and p.get("Remote") != "yes":
                graphical.append(p)
    else:
        info("loginctl is not available; looking at the processes instead")

    env, where = find_session(display_override)
    if env is None:
        fail(
            f"no desktop session of {name} found",
            f"log in on the board's screen as {name} -- the kiosk opens in "
            "that session -- then run this again",
        )
        return None
    shown = ", ".join(
        f"{k}={env[k]}"
        for k in (
            "XDG_SESSION_TYPE",
            "XDG_CURRENT_DESKTOP",
            "DISPLAY",
            "WAYLAND_DISPLAY",
        )
        if env.get(k)
    )
    ok(f"desktop session found through {where}: {shown}")
    if code == 0 and not graphical and not display_override:
        warn("loginctl lists no local graphical session for this user")
    if not os.path.isdir(os.path.expanduser("~/.config/autostart")):
        info(
            "~/.config/autostart does not exist yet; the kiosk installer "
            "creates it"
        )
    return env


def autologin_user(manager):
    """The user the display manager logs in at boot, or "" if none is set,
    or None if its configuration could not be read."""
    if manager.startswith("gdm"):
        files = ["/etc/gdm/custom.conf", "/etc/gdm3/custom.conf"]
        enabled_key, user_key = r"AutomaticLoginEnable", r"AutomaticLogin"
    elif manager.startswith("sddm"):
        files = ["/etc/sddm.conf"] + sorted(
            glob.glob("/etc/sddm.conf.d/*.conf")
        )
        enabled_key, user_key = None, r"User"
    elif manager.startswith("lightdm"):
        files = ["/etc/lightdm/lightdm.conf"] + sorted(
            glob.glob("/etc/lightdm/lightdm.conf.d/*.conf")
        )
        enabled_key, user_key = None, r"autologin-user"
    elif manager.startswith("lxdm"):
        files = ["/etc/lxdm/lxdm.conf"]
        enabled_key, user_key = None, r"autologin"
    else:
        return None
    texts = [read(f) for f in files if os.path.isfile(f)]
    if not texts:
        return None
    text = "\n".join(texts)
    if enabled_key:
        m = re.search(rf"^\s*{enabled_key}\s*=\s*(\S+)", text, re.M | re.I)
        if not m or m.group(1).lower() not in ("true", "1", "yes"):
            return ""
    m = re.search(rf"^\s*{user_key}\s*=\s*(\S+)", text, re.M)
    return m.group(1) if m else ""


def check_login(name):
    say("login at boot")
    target = os.path.realpath("/etc/systemd/system/display-manager.service")
    manager = os.path.basename(target) if os.path.exists(target) else ""
    if not manager:
        warn(
            "no display-manager.service: could not tell how the desktop logs in"
        )
        return
    user = autologin_user(manager)
    if user is None:
        warn(
            f"{manager}: its configuration could not be read; automatic login not checked"
        )
    elif user == name:
        ok(f"{manager} logs {name} in at boot")
    elif user:
        warn(
            f"{manager} logs {user} in at boot, not {name}",
            f"the kiosk is installed for {name}; log that user in instead",
        )
    else:
        warn(
            f"{manager} does not log anyone in at boot: after a reboot the "
            "screen waits at the login prompt and the kiosk does not start",
            f"turn on automatic login for {name} in the desktop's settings",
        )


def check_browsers(override):
    say("browser")
    found = []
    candidates = [override] if override else list(BROWSERS)
    for name in candidates:
        path = name if os.path.sep in name else shutil.which(name)
        if not path or not os.access(path, os.X_OK):
            if override:
                fail(f"--browser {override} is not an executable")
            continue
        _, out = run([path, "--version"], timeout=20)
        version = (
            out.strip().splitlines()[0] if out.strip() else "version unknown"
        )
        ok(f"{os.path.basename(path)}: {version} ({path})")
        found.append([path])
    if not override and shutil.which("flatpak"):
        _, out = run(["flatpak", "list", "--app", "--columns=application"])
        for app in FLATPAKS:
            if app in out.split():
                ok(f"flatpak {app}")
                found.append(["flatpak", "run", app])
    if not found:
        fail(
            "no browser found (" + ", ".join(BROWSERS) + ", or a flatpak)",
            "say which browser the board has, or pass --browser /path/to/it",
        )
        return None
    return found[0]


def check_fonts():
    say("fonts")
    code, out = run(["fc-list", ":lang=zh-tw", "family"])
    if code == 127:
        warn("fc-list is not installed: fonts not checked")
        return
    families = sorted(
        {
            line.split(",")[0].strip()
            for line in out.splitlines()
            if line.strip()
        }
    )
    if families:
        ok(
            f"{len(families)} font families with Traditional Chinese, e.g. "
            + ", ".join(families[:3])
        )
    else:
        fail(
            "no font with Traditional Chinese: Chinese on the screen shows "
            "as boxes",
            "install one once, with the internet: dnf search noto-sans-cjk",
        )
    _, out = run(["fc-match", "sans-serif:lang=zh-tw", "family"])
    if out.strip():
        info(f"sans-serif in Traditional Chinese resolves to {out.strip()}")


def check_blanking(env):
    say("blanking")
    if env is None:
        info("no desktop session: not checked")
        return
    child = dict(os.environ, **env)
    told = False
    desktop = env.get("XDG_CURRENT_DESKTOP", "")
    if "GNOME" in desktop.upper() and shutil.which("gsettings"):
        _, delay = run(
            ["gsettings", "get", "org.gnome.desktop.session", "idle-delay"],
            child,
        )
        _, lock = run(
            [
                "gsettings",
                "get",
                "org.gnome.desktop.screensaver",
                "lock-enabled",
            ],
            child,
        )
        m = re.search(r"(\d+)", delay)
        if m:
            told = True
            seconds = int(m.group(1))
            if seconds:
                warn(
                    f"GNOME blanks the screen after {seconds} s idle"
                    + (" and locks it" if lock.strip() == "true" else ""),
                    "the kiosk launcher turns this off; until then the screen "
                    "goes dark while a meeting is processed",
                )
            else:
                ok("GNOME never blanks the screen (idle-delay 0)")
    if env.get("DISPLAY") and shutil.which("xset"):
        code, out = run(["xset", "q"], child)
        if code == 0:
            told = True
            saver = re.search(r"timeout:\s+(\d+)", out)
            dpms = "DPMS is Enabled" in out
            info(
                f"X screen saver timeout {saver.group(1) if saver else '?'} s; "
                f"DPMS {'on' if dpms else 'off'}"
                + (
                    " (Xwayland: the compositor may decide instead)"
                    if env.get("WAYLAND_DISPLAY")
                    else ""
                )
            )
    if not told:
        info(
            f"could not read the blanking settings of {desktop or 'this desktop'}; "
            "watch whether the screen goes dark during the kiosk test"
        )


def check_input():
    say("input")
    ignore = re.compile(
        r"power button|sleep button|video bus|gpio|pwrkey|cec|lid switch|"
        r"headphone|hdmi|audio|ir[-_ ]|vc4|rtc",
        re.I,
    )
    devices = []
    for block in read("/proc/bus/input/devices").split("\n\n"):
        name = re.search(r'^N: Name="(.*)"', block, re.M)
        handlers = re.search(r"^H: Handlers=(.*)", block, re.M)
        if not (name and handlers):
            continue
        kinds = [k for k in ("kbd", "mouse") if k in handlers.group(1)]
        if kinds and not ignore.search(name.group(1)):
            devices.append(f"{name.group(1)} ({'+'.join(kinds)})")
    if devices:
        for d in dict.fromkeys(devices):
            ok(d)
    else:
        warn(
            "no keyboard, mouse or presenter remote plugged in",
            "the screen switches between the live meeting and the showcase "
            "with PageDown / PageUp; plug in a remote or a keyboard for the "
            "demo",
        )


def check_service():
    say("service")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", SCREEN_PORT))
            ok(f"port {SCREEN_PORT} is free for the screen service")
        except OSError as err:
            fail(
                f"port {SCREEN_PORT} is taken ({err.strerror})",
                f"ss -ltnp | grep {SCREEN_PORT}",
            )

    where = os.path.expanduser(
        setting(dotenv(), "MEETINGS_DIR", "/tmp/meetings")
    )
    fs = filesystem_of(where)
    if fs == "tmpfs":
        warn(
            f"meetings land in {where}, on tmpfs: they are gone after a "
            "reboot, the showcase meeting with them",
            "set MEETINGS_DIR in ai_agents/.env to a disk path",
        )
    else:
        ok(f"meetings land in {where}, on {fs}")
    free = free_mb(where)
    if free < 250 + 64:
        warn(f"{free:.0f} MB free under {where}; a meeting needs about 300")
    else:
        ok(f"{free:.0f} MB free under {where}")

    _, out = run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    addresses = re.findall(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)", out, re.M)
    if addresses:
        info(
            "the phone app reaches the board at "
            + ", ".join(f"{ip} ({dev})" for dev, ip in addresses)
        )

    busy = []
    if answers(f"{UPLOADER}/meetings"):
        for path in glob.glob(os.path.join(where, "*", "work", "state.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    state = json.load(f).get("state")
            except (OSError, ValueError):
                continue
            if state not in FINAL:
                busy.append(
                    os.path.basename(os.path.dirname(os.path.dirname(path)))
                )
    if busy:
        info(f"a meeting is being processed now: {', '.join(busy)}")
    return busy


# --------------------------------------------------------------------------
# the kiosk test


TEST_PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><title>screen probe</title>
<style>
  html, body { margin: 0; height: 100%; background: #0b1120; color: #f1f5f9;
    font-family: sans-serif; overflow: hidden; }
  main { padding: 64px; }
  h1 { font-size: 72px; margin: 0 0 24px; }
  p { font-size: 36px; margin: 0 0 16px; color: #cbd5e1; }
  #size { font-size: 48px; color: #fbbf24; }
  .swatches { display: flex; gap: 16px; margin: 40px 0; }
  .swatches div { width: 120px; height: 80px; border-radius: 12px; }
  .bar { height: 24px; width: 30%; border-radius: 12px; background: #38bdf8;
    animation: grow 1.5s ease-in-out 2 alternate; }
  @keyframes grow { to { width: 90%; } }
</style></head>
<body><main>
  <h1>會議紀錄大屏 · 螢幕測試</h1>
  <p>看得到這兩行中文，字型就沒問題。測試結束會自己關掉。</p>
  <p id="size">…</p>
  <div class="swatches">
    <div style="background:#38bdf8"></div><div style="background:#f472b6"></div>
    <div style="background:#a3e635"></div><div style="background:#fbbf24"></div>
    <div style="background:#c084fc"></div><div style="background:#fb7185"></div>
    <div style="background:#2dd4bf"></div><div style="background:#f97316"></div>
    <div style="background:#60a5fa"></div><div style="background:#e879f9"></div>
  </div>
  <div class="bar"></div>
</main>
<script>
  const size = () => `${screen.width}x${screen.height} 螢幕 · ` +
    `${innerWidth}x${innerHeight} 頁面 · 縮放 ${devicePixelRatio}`;
  document.getElementById("size").textContent = size();
  let frames = 0;
  const start = performance.now();
  function tick(now) {
    frames++;
    if (now - start < 3000) { requestAnimationFrame(tick); return; }
    fetch("/report", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        screen: [screen.width, screen.height],
        viewport: [innerWidth, innerHeight],
        dpr: devicePixelRatio,
        visible: document.visibilityState,
        focus: document.hasFocus(),
        fps: frames * 1000 / (now - start),
        ua: navigator.userAgent,
      })});
    document.getElementById("size").textContent = size();
  }
  requestAnimationFrame(tick);
  // What the screen will do all day: ask the board every two seconds.
  setInterval(() => fetch("/poll", {cache: "no-store"}).catch(() => {}), 2000);
</script></body></html>
"""


class _Probe(http.server.BaseHTTPRequestHandler):
    report = None
    served_at = None
    polls = 0

    def log_message(self, *_):
        pass

    def _send(self, code, body, kind):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # pylint: disable=invalid-name
        if self.path == "/":
            if _Probe.served_at is None:
                _Probe.served_at = time.monotonic()
            self._send(200, TEST_PAGE, "text/html; charset=utf-8")
        elif self.path.startswith("/poll"):
            _Probe.polls += 1
            self._send(200, json.dumps({"t": time.time()}), "application/json")
        else:
            self._send(404, "", "text/plain")

    def do_POST(self):  # pylint: disable=invalid-name
        length = int(self.headers.get("Content-Length") or 0)
        try:
            _Probe.report = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            _Probe.report = {}
        self._send(204, "", "text/plain")


def kiosk_command(browser, url, profile):
    name = os.path.basename(browser[-1]).lower()
    if "firefox" in name:
        with open(os.path.join(profile, "user.js"), "w", encoding="utf-8") as f:
            for key, value in FIREFOX_PREFS.items():
                f.write(f"user_pref({json.dumps(key)}, {json.dumps(value)});\n")
        return browser + ["--kiosk", "--no-remote", "--profile", profile, url]
    if "chrom" in name:
        return (
            browser + list(CHROMIUM_FLAGS) + [f"--user-data-dir={profile}", url]
        )
    return browser + [url]


def stop_browser(sid, profile):
    pids = session_members(sid)
    # Anything that left the session still names the profile, whose path is
    # unique to this run, on its command line.
    for entry in own_processes():
        if entry not in pids and entry != os.getpid():
            if profile in read(f"/proc/{entry}/cmdline").replace("\0", " "):
                pids.append(entry)
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(proc_stat(p) for p in pids):
        time.sleep(0.2)
    for p in pids:
        if proc_stat(p):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


def check_kiosk(args, env, browser):
    say("kiosk")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Probe)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = args.url or f"http://127.0.0.1:{server.server_address[1]}/"
    profile = tempfile.mkdtemp(prefix="probe-demo-screen-")
    cmd = kiosk_command(browser, url, profile)
    child = {k: v for k, v in os.environ.items() if k not in SESSION_KEYS}
    child.update(env)
    errors = os.path.join(profile, "stderr.log")
    info("opening " + " ".join(cmd))

    before = mem_available_mb()
    lowest = before
    clk = os.sysconf("SC_CLK_TCK")
    started = time.monotonic()
    with open(errors, "wb") as err_out:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            cmd,
            env=child,
            stdout=subprocess.DEVNULL,
            stderr=err_out,
            start_new_session=True,
        )
    sid = proc.pid
    peak = 0.0
    loaded = None  # when the page reported; with --url, after it settled
    ticks_at_load = 0
    # The test page reports by itself; another page is given 10 s to load.
    settle_until = started + (10 if args.url else LOAD_TIMEOUT_S)
    until = None
    try:
        while True:
            now = time.monotonic()
            proc.poll()
            members = session_members(sid)
            peak = max(peak, sum(memory_mb(p) for p in members))
            lowest = min(lowest, mem_available_mb())
            if loaded is None:
                ready = (
                    now >= settle_until
                    if args.url
                    else _Probe.report is not None
                )
                if ready:
                    loaded = now
                    ticks_at_load = cpu_ticks(members)
                    until = now + args.seconds
                elif now >= settle_until:
                    break
            elif now >= until:
                break
            if not members:
                break
            time.sleep(1)
        ticks_end = cpu_ticks(session_members(sid))
        ended = time.monotonic()
        gone = not session_members(sid)
    finally:
        stop_browser(sid, profile)
        proc.poll()
        server.shutdown()

    if gone and (loaded is None or ended < until):
        tail = read(errors).strip().splitlines()[-5:]
        shutil.rmtree(profile, ignore_errors=True)
        fail(
            f"the browser exited (code {proc.returncode}) "
            + (
                "before the page loaded"
                if loaded is None
                else "while the page was up"
            ),
            "its last words: " + (" | ".join(tail) if tail else "(nothing)"),
        )
        return
    shutil.rmtree(profile, ignore_errors=True)

    if not args.url:
        if _Probe.report is None:
            fail(
                f"the test page did not report within {LOAD_TIMEOUT_S} s",
                "the browser may have opened somewhere else, or stopped at a "
                "prompt; look at the screen while it runs",
            )
            return
        rep = _Probe.report
        took = (_Probe.served_at or loaded) - started
        ok(f"page requested {took:.1f} s after the browser was started")
        sw, sh = rep.get("screen", [0, 0])
        vw, vh = rep.get("viewport", [0, 0])
        dpr = rep.get("dpr", 1)
        if abs(sw - vw) <= 2 and abs(sh - vh) <= 2:
            ok(
                f"full screen: the page fills the {sw}x{sh} screen (scale {dpr})"
            )
        else:
            fail(
                f"not full screen: the page is {vw}x{vh} on a {sw}x{sh} screen",
                "the browser did not honour --kiosk; say which browser it is",
            )
        if rep.get("visible") == "visible" and rep.get("focus"):
            ok("the page is in front and has the focus")
        else:
            warn(
                f"the page is {rep.get('visible')}, focus {rep.get('focus')}: "
                "something covers it (a first-run tab, a dialog, another window)"
            )
        fps = float(rep.get("fps") or 0)
        if fps >= 50:
            ok(f"{fps:.0f} frames per second while animating")
        elif fps >= 30:
            warn(
                f"{fps:.0f} frames per second while animating: transitions will look rough"
            )
        else:
            warn(
                f"{fps:.0f} frames per second while animating: keep the screen free of animation"
            )
        info(f"user agent: {rep.get('ua', '?')}")
        info(
            f"the page polled {_Probe.polls} times, as the screen will every 2 s"
        )

    span = max(ended - (loaded or ended), 1e-6)
    cpu = max(ticks_end - ticks_at_load, 0) / clk / span
    total = mem_total_mb()
    ok(
        f"the browser took {peak:.0f} MB at most (proportional set size) and "
        f"{cpu * 100:.0f}% of one core over {span:.0f} s with the page up"
    )
    headroom = lowest - MEETING_PEAK_MB
    info(
        f"memory available: {before:.0f} MB before, {lowest:.0f} MB at the "
        f"lowest, of {total:.0f} MB"
    )
    if headroom < 500:
        warn(
            f"with the browser up, a meeting peaking at {MEETING_PEAK_MB} MB "
            f"would leave {headroom:.0f} MB",
        )
    else:
        ok(
            f"with the browser up, a meeting peaking at {MEETING_PEAK_MB} MB "
            f"still leaves about {headroom:.0f} MB"
        )
    if cpu > 0.25:
        warn(
            f"the waiting page keeps {cpu * 100:.0f}% of a core busy; diarization "
            "uses all four, so processing would slow down"
        )


# --------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(
        description="Check what the meeting demo screen relies on, on the board."
    )
    p.add_argument(
        "--no-kiosk", action="store_true", help="skip the browser test"
    )
    p.add_argument(
        "--seconds",
        type=int,
        default=30,
        help="how long the test page stays up (30)",
    )
    p.add_argument(
        "--url",
        help="open this page instead of the test page, and only measure",
    )
    p.add_argument(
        "--browser", help="the browser to use, if not the first one found"
    )
    p.add_argument(
        "--display", help="an X display to use instead of the session's"
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="run the browser test even while a meeting is processed",
    )
    args = p.parse_args()

    name = check_user()
    if name is not None:
        check_display()
        env = check_session(name, args.display)
        check_login(name)
        browser = check_browsers(args.browser)
        check_fonts()
        check_blanking(env)
        check_input()
        busy = check_service()
        if args.no_kiosk:
            say("kiosk")
            info("skipped (--no-kiosk)")
        elif env is None or browser is None:
            say("kiosk")
            info("skipped: it needs a desktop session and a browser")
        elif busy and not args.force:
            say("kiosk")
            warn(
                "skipped: a meeting is being processed and the browser would "
                "take CPU from it",
                "run this again when it is done, or with --force",
            )
        else:
            check_kiosk(args, env, browser)

    say("summary")
    for msg in failures:
        emit(f"  FAIL  {msg}")
    for msg in warnings:
        emit(f"  warn  {msg}")
    if not failures and not warnings:
        emit("  everything held")
    path = os.path.join(
        tempfile.gettempdir(), f"probe_demo_screen-{os.getuid()}.log"
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines) + "\n")
        emit(f"\n  log: {path}")
    except OSError as err:
        emit(f"\n  no log written ({err})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
