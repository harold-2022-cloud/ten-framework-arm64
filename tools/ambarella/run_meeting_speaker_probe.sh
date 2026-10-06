#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Run probe_meeting_speakers.py on the board, end to end, from this machine:
# prepare the test meeting here, ship it, run it there, bring results back.
#
#   tools/ambarella/run_meeting_speaker_probe.sh           # run, or reattach
#   tools/ambarella/run_meeting_speaker_probe.sh --fresh   # discard, rerun
#
# One password prompt. Every ssh and scp goes through a single master
# connection, closed on exit.
#
# The probe runs under nohup on the board, so a dropped connection or a
# Ctrl-C here does not stop it. Run this again and it reattaches to the log,
# or collects the results if the run already finished.
#
# Models missing on the board are fetched by install_meeting_models.sh,
# shipped alongside. That needs the board to reach github.com.
#
# Test data: one AISHELL-4 test session, M_R003S01C01. It is a real Mandarin
# meeting: 6 speakers, 38 minutes, recorded on a far-field 8-channel array.
# Only channel 1 is used, passed through 16 kbps Ogg-Opus and back, so the
# board hears what an uploaded meeting would sound like. The reference RTTMs
# and this session lead the 5.2 GB test tarball, so preparing it streams
# about 260 MB, not 5.2 GB.
#
# Environment:
#   BOARD        ssh target                  (default lychee@192.168.0.19)
#   BOARD_PY     python with sherpa_onnx     (default python3)
#   DATA_DIR     test meeting, on this side  (default ~/meeting_probe/aishell4)
#   RESULTS_DIR  results, on this side       (default ~/meeting_probe/results)
#   PROBE_ARGS   extra probe arguments, e.g. "--minutes 10 --threads 2"
#
set -euo pipefail

BOARD="${BOARD:-lychee@192.168.0.19}"
BOARD_PY="${BOARD_PY:-python3}"
DATA_DIR="${DATA_DIR:-$HOME/meeting_probe/aishell4}"
RESULTS_DIR="${RESULTS_DIR:-$HOME/meeting_probe/results}"
PROBE_ARGS="${PROBE_ARGS:-}"
SESSION="M_R003S01C01"
URL="https://openslr.trmal.net/resources/111/test.tar.gz"
REMOTE="meeting_probe" # relative to the board user's home

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$HERE/probe_meeting_speakers.py"

FRESH=0
case "${1:-}" in
  --fresh) FRESH=1 ;;
  "") ;;
  *) echo "usage: $0 [--fresh]" >&2; exit 2 ;;
esac

say() { printf '\n\033[1m===== %s\033[0m\n' "$*"; }
ok() { printf '  \033[32mok\033[0m    %s\n' "$*"; }
info() { printf '        %s\n' "$*"; }
die() { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------------ 1
say "1. This machine"

for t in ssh scp python3; do have "$t" || die "$t is required here"; done
[[ -f "$PROBE" ]] || die "expected $PROBE"
ok "ssh, scp, python3, $(basename "$PROBE")"

WAV="$DATA_DIR/$SESSION.wav"
RTTM="$DATA_DIR/$SESSION.rttm"
if [[ -s "$WAV" && -s "$RTTM" ]]; then
  ok "test meeting already prepared in $DATA_DIR"
else
  have ffmpeg || die "ffmpeg is required here to prepare the test meeting"
  mkdir -p "$DATA_DIR"
  info "streaming $SESSION and its reference out of the AISHELL-4 test set"
  python3 - "$URL" "$DATA_DIR" "$SESSION" <<'EOF'
import os, shutil, sys, tarfile, urllib.request

url, out, session = sys.argv[1:4]
want = {f"test/TextGrid/{session}.rttm": f"{session}.rttm",
        f"test/wav/{session}.flac": f"{session}.flac"}
archive = tarfile.open(fileobj=urllib.request.urlopen(url), mode="r|gz")
for member in archive:
    if member.name in want:
        with archive.extractfile(member) as src, \
                open(os.path.join(out, want.pop(member.name)), "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        if not want:
            break
if want:
    sys.exit(f"not found in the archive: {sorted(want)}")
EOF
  info "channel 1 -> Ogg-Opus 16 kbps -> 16 kHz mono wav"
  ffmpeg -hide_banner -loglevel error -y -i "$DATA_DIR/$SESSION.flac" \
    -af "pan=mono|c0=c0" -ar 16000 -c:a libopus -b:a 16k -application voip \
    "$DATA_DIR/$SESSION.ogg"
  ffmpeg -hide_banner -loglevel error -y -i "$DATA_DIR/$SESSION.ogg" \
    -ar 16000 -ac 1 -c:a pcm_s16le "$WAV"
  [[ -s "$WAV" && -s "$RTTM" ]] || die "preparation produced no $WAV / $RTTM"
  ok "prepared $WAV"
fi

# ------------------------------------------------------------------ 2
say "2. Connecting to $BOARD (one password prompt)"

SOCK="$(mktemp -u "${TMPDIR:-/tmp}/mprobe.XXXXXX")"
ssh -o ControlMaster=yes -o ControlPath="$SOCK" -o ControlPersist=no \
  -o ServerAliveInterval=30 -o ConnectTimeout=10 -fN "$BOARD" ||
  die "cannot open an ssh connection to $BOARD"
trap 'ssh -o ControlPath="$SOCK" -O exit "$BOARD" >/dev/null 2>&1 || true' EXIT
R() { ssh -o ControlPath="$SOCK" "$BOARD" "$@"; }
CP() { scp -q -o ControlPath="$SOCK" "$@"; }
ok "connected"

R "mkdir -p ~/$REMOTE"
state() {
  R "cd ~/$REMOTE; if [ -f job.pid ] && kill -0 \$(cat job.pid) 2>/dev/null;
     then echo running; elif [ -f exit_code ]; then echo done; else echo none; fi"
}
STATE="$(state)"

if [[ "$FRESH" -eq 1 && "$STATE" == "running" ]]; then
  info "--fresh: stopping the run in progress"
  # The python is a child of the sh wrapper whose pid is in job.pid; kill
  # only that tree, not every probe on the machine.
  R "P=\$(cat ~/$REMOTE/job.pid); pkill -P \$P 2>/dev/null; kill \$P 2>/dev/null; true"
  STATE="none"
fi
if [[ "$FRESH" -eq 1 ]]; then STATE="none"; fi
if [[ "$STATE" == "done" ]]; then
  info "a finished run is already on the board; collecting it"
  info "(run with --fresh to discard it and run again)"
fi

# ------------------------------------------------------------------ 3
if [[ "$STATE" == "none" ]]; then
  say "3. Shipping and checking the board"

  CP "$PROBE" "$HERE/install_meeting_models.sh" "$HERE/probe_diarization.py" \
    "$BOARD:$REMOTE/"
  for f in "$WAV" "$RTTM"; do
    local_size="$(stat -c %s "$f")"
    remote_size="$(R "stat -c %s ~/$REMOTE/$(basename "$f") 2>/dev/null || echo 0")"
    if [[ "$local_size" != "$remote_size" ]]; then
      info "copying $(basename "$f") ($((local_size / 1048576)) MB)"
      CP "$f" "$BOARD:$REMOTE/"
    fi
  done
  ok "probe, installer and test meeting on the board in ~/$REMOTE"

  CHECK="cd ~/$REMOTE && $BOARD_PY probe_meeting_speakers.py --check \
    --audio $SESSION.wav --rttm $SESSION.rttm $PROBE_ARGS"
  if ! R "$CHECK"; then
    D="~/diarization_models"
    if R "test -f $D/sherpa-onnx-pyannote-segmentation-3-0/model.onnx &&
      test -f $D/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx &&
      ls ~/sensevoice/model*.onnx >/dev/null 2>&1"; then
      die "the board's preflight failed with the models in place -- see the
  output above (BOARD_PY must be a python that imports sherpa_onnx)"
    fi
    info "models missing; running install_meeting_models.sh on the board"
    R "cd ~/$REMOTE && bash install_meeting_models.sh" ||
      die "install_meeting_models.sh failed on the board -- see above"
    R "$CHECK" || die "preflight still fails after installing the models"
  fi

  say "4. Starting the probe on the board"
  # One command per line: "a && b && cmd &" would background the whole
  # list, and the echo below would then write job.pid outside ~/$REMOTE.
  R "cd ~/$REMOTE || exit 1
     rm -f exit_code results.json run.log
     nohup sh -c '$BOARD_PY -u probe_meeting_speakers.py \
       --audio $SESSION.wav --rttm $SESSION.rttm --out results.json \
       $PROBE_ARGS > run.log 2>&1; echo \$? > exit_code' >/dev/null 2>&1 &
     echo \$! > job.pid"
  ok "started; the full meeting takes roughly 40-70 minutes on the board"
  STATE="running"
fi

# ------------------------------------------------------------------ 5
if [[ "$STATE" == "running" ]]; then
  say "5. Following the run (Ctrl-C only detaches; the board keeps going)"
  info "Run this script again to reattach, or to collect the results."
  if ! R "tail -n +1 -f --pid=\$(cat ~/$REMOTE/job.pid) ~/$REMOTE/run.log"; then
    [[ "$(state)" == "running" ]] &&
      die "lost the connection; the probe is still running on the board.
  Run this script again to reattach."
  fi
fi

# ------------------------------------------------------------------ 6
say "6. Collecting the results"

CODE="$(R "cat ~/$REMOTE/exit_code 2>/dev/null || echo missing")"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$RESULTS_DIR/$STAMP"
mkdir -p "$OUT"
CP "$BOARD:$REMOTE/run.log" "$OUT/" || true
CP "$BOARD:$REMOTE/results.json" "$OUT/" 2>/dev/null || true
ln -sfn "$STAMP" "$RESULTS_DIR/latest"

[[ "$CODE" == "0" ]] || die "the probe exited with '$CODE' -- see $OUT/run.log"
ok "results in $OUT"
ok "also at $RESULTS_DIR/latest -- tell Claude it finished"
