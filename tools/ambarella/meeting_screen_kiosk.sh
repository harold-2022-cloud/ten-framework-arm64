#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# The meeting demo screen, full screen, as the desktop starts: waits for the
# screen's service, keeps the display awake while it shows, and opens the
# page in a browser with a profile of its own. The desktop's autostart runs
# it (tools/ambarella/install_board_services.sh --kiosk on). Alt+F4 leaves it.
#
#   tools/ambarella/meeting_screen_kiosk.sh
#
# Its log, /tmp/meeting_screen_kiosk.log, says how long the service took,
# how the display is kept awake and which browser opened.
#
# Settings, from the environment: SCREEN_URL (http://127.0.0.1:8766/),
# KIOSK_LOG, KIOSK_PROFILE (~/.cache/meeting-screen-browser).

set -uo pipefail

URL="${SCREEN_URL:-http://127.0.0.1:8766/}"
LOG="${KIOSK_LOG:-/tmp/meeting_screen_kiosk.log}"
PROFILE="${KIOSK_PROFILE:-$HOME/.cache/meeting-screen-browser}"

exec >> "$LOG" 2>&1 < /dev/null
log() { echo "$(date '+%F %T') $*"; }
log "start: session ${XDG_SESSION_TYPE:-?}, desktop ${XDG_CURRENT_DESKTOP:-?}"

# The page first: a browser opened before the service answers shows an
# error page, and that page does not try again.
waited=0
until curl -s -m 2 -o /dev/null "$URL"; do
  sleep 2
  waited=$((waited + 2))
  ((waited % 60 == 0)) && log "still waiting for $URL after $waited s"
done
log "$URL answers after $waited s"

# The display kept awake while the page shows. KDE Plasma -- the board's
# desktop, on Wayland, where xset reaches nothing -- through kde-inhibit
# around the browser, which holds off dimming, the screen turning off and
# the lock for as long as it runs; X11 through xset; GNOME through gsettings.
awake=()
if [[ "${XDG_CURRENT_DESKTOP:-}" == *KDE* ]] && command -v kde-inhibit >/dev/null; then
  awake=(kde-inhibit --power --screenSaver)
  log "display kept awake by kde-inhibit --power --screenSaver while the browser runs"
elif [[ "${XDG_SESSION_TYPE:-}" == x11 ]] && command -v xset >/dev/null; then
  xset s off -dpms && log "display kept awake: xset s off -dpms"
elif [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]] && command -v gsettings >/dev/null; then
  gsettings set org.gnome.desktop.session idle-delay 0 &&
    gsettings set org.gnome.desktop.screensaver lock-enabled false &&
    log "display kept awake: GNOME idle-delay 0, lock off"
else
  log "no way found to keep the display awake here: it may dim, turn off or lock"
fi

mkdir -p "$PROFILE"
if command -v firefox >/dev/null; then
  # Its own profile, told to ask nothing: no welcome, no restore after a
  # power cut, no default-browser question, no update prompt.
  cat > "$PROFILE/user.js" <<'EOF'
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("app.update.auto", false);
user_pref("browser.translations.automaticallyPopup", false);
EOF
  log "opening $URL in firefox"
  exec "${awake[@]}" firefox --kiosk --no-remote --profile "$PROFILE" "$URL"
fi
for browser in chromium chromium-browser google-chrome; do
  if command -v "$browser" >/dev/null; then
    log "opening $URL in $browser"
    exec "${awake[@]}" "$browser" --kiosk --noerrdialogs --disable-session-crashed-bubble \
      --no-first-run --user-data-dir="$PROFILE" "$URL"
  fi
done
log "no browser found (firefox, chromium, chromium-browser, google-chrome)"
exit 1
