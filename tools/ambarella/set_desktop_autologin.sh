#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Log the board's desktop user in at power-on, so that what starts with the
# desktop -- the meeting demo screen's kiosk -- comes up with no one at the
# board. Without it the screen waits at the login prompt after every boot.
#
#   tools/ambarella/set_desktop_autologin.sh                    on: you, into the
#                                                               desktop you last chose
#   tools/ambarella/set_desktop_autologin.sh --session plasma   on, into that desktop
#   tools/ambarella/set_desktop_autologin.sh --status           what is set
#   tools/ambarella/set_desktop_autologin.sh --off              off again
#
# The board's login screen is SDDM; this knows no other. The setting is one
# file of this script's own, /etc/sddm.conf.d/zz-autologin.conf (read after
# the others there), and --off removes it; nothing else is edited. It takes
# effect at the next boot.
#
# Run it as the desktop user (lychee), not with sudo; it asks sudo to write
# the file. Anyone at the board's screen then has that desktop, unlocked.
#
# Settings, from the environment: SDDM_CONF_D (/etc/sddm.conf.d), SDDM_CONF
# (/etc/sddm.conf), SDDM_STATE (/var/lib/sddm/state.conf), SESSION_DIRS
# ("/usr/share/wayland-sessions /usr/share/xsessions").

set -uo pipefail

CONF_D="${SDDM_CONF_D:-/etc/sddm.conf.d}"
MAIN="${SDDM_CONF:-/etc/sddm.conf}"
STATE="${SDDM_STATE:-/var/lib/sddm/state.conf}"
SESSION_DIRS="${SESSION_DIRS:-/usr/share/wayland-sessions /usr/share/xsessions}"
OURS="$CONF_D/zz-autologin.conf"
ME=$(id -un)

ok() { echo "  ok    $*"; }
note() { echo "  note  $*"; }
fail() { echo "  FAIL  $*"; exit 1; }

MODE=on
SESSION=""
while (($#)); do
  case "$1" in
    --session) [[ -n "${2:-}" ]] || { echo "--session needs a desktop's name" >&2; exit 2; }; SESSION="$2"; shift ;;
    --status) MODE=status ;;
    --off) MODE=off ;;
    -h|--help) sed -n '6,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

# A key of a section in SDDM's ini files, the last file that sets it winning,
# as SDDM reads them: the conf.d files in order, then the main one.
setting() { # section key
  local f files=()
  for f in "$CONF_D"/*.conf; do [[ -f "$f" ]] && files+=("$f"); done
  [[ -f "$MAIN" ]] && files+=("$MAIN")
  ((${#files[@]})) || return 0
  awk -v want="[$1]" -v key="$2" '
    FNR == 1 { section = "" }
    /^[[:space:]]*\[/ { section = $0; gsub(/[[:space:]]/, "", section); next }
    section == want && $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      value = $0; sub(/^[^=]*=[[:space:]]*/, "", value); sub(/[[:space:]]*$/, "", value); found = value
    }
    END { printf "%s", found }' "${files[@]}"
}

# The desktops there are to log into: the session files' names.
sessions() {
  local d f
  for d in $SESSION_DIRS; do
    for f in "$d"/*.desktop; do [[ -f "$f" ]] && basename "$f" .desktop; done
  done | sort -u
}

# The desktop SDDM logged someone into last, as its state file names it.
last_session() {
  local text
  text=$(cat "$STATE" 2>/dev/null || sudo cat "$STATE" 2>/dev/null) || return 0
  awk '/^[[:space:]]*\[/ { s = $0 } s ~ /\[Last\]/ && /^[[:space:]]*Session[[:space:]]*=/ {
    v = $0; sub(/^[^=]*=[[:space:]]*/, "", v); n = split(v, p, "/"); v = p[n]; sub(/\.desktop$/, "", v); print v }' <<< "$text" | tail -1
}

status() {
  local user session
  user=$(setting Autologin User)
  session=$(setting Autologin Session)
  if [[ -n "$user" ]]; then
    ok "autologin: $user into ${session%.desktop} at boot"
  else
    note "autologin: off -- the screen waits at the login prompt after a boot"
  fi
  [[ -f "$OURS" ]] && note "set by $OURS"
  return 0
}

[[ $(id -u) -ne 0 ]] || fail "run it as the desktop user (lychee), not with sudo"

if [[ $MODE == status ]]; then
  status
  exit 0
fi

if [[ $MODE == off ]]; then
  if [[ -f "$OURS" ]]; then
    sudo rm -f "$OURS" || fail "could not remove $OURS"
    ok "removed $OURS"
  else
    ok "nothing to remove: there is no $OURS"
  fi
  status
  exit 0
fi

systemctl is-active --quiet sddm ||
  fail "the board's login screen is not SDDM (sddm.service is not running); this script knows only SDDM"

mapfile -t have < <(sessions)
((${#have[@]})) || fail "no desktop sessions in $SESSION_DIRS"
list=$(printf '%s, ' "${have[@]}")
list=${list%, }
if [[ -n "$SESSION" ]]; then
  [[ " ${have[*]} " == *" $SESSION "* ]] || fail "no desktop called $SESSION; there are: $list"
else
  last=$(last_session)
  if [[ -n "$last" && " ${have[*]} " == *" $last "* ]]; then
    SESSION="$last"
    ok "the desktop $ME last chose: $SESSION"
  elif ((${#have[@]} == 1)); then
    SESSION="${have[0]}"
    ok "the only desktop there is: $SESSION"
  else
    fail "several desktops ($list) and none chosen before: pick one with --session NAME"
  fi
fi

# The main file is read last: a User there would win over ours.
main_user=$(awk '
  /^[[:space:]]*\[/ { s = $0; gsub(/[[:space:]]/, "", s); next }
  s == "[Autologin]" && /^[[:space:]]*User[[:space:]]*=/ { v = $0; sub(/^[^=]*=[[:space:]]*/, "", v); print "set:" v }' "$MAIN" 2>/dev/null | tail -1)
if [[ -n "$main_user" && "${main_user#set:}" != "$ME" ]]; then
  fail "$MAIN sets [Autologin] User=${main_user#set:}, which wins over $OURS: remove that line first"
fi

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<EOF
# Written by tools/ambarella/set_desktop_autologin.sh; --off removes it.
[Autologin]
User=$ME
Session=$SESSION.desktop
Relogin=false
EOF
sudo install -d -m 755 "$CONF_D" && sudo install -m 644 "$tmp" "$OURS" || fail "could not write $OURS"
ok "wrote $OURS: $ME into $SESSION at boot"
status
echo
echo "  It takes effect at the next boot: sudo reboot"
