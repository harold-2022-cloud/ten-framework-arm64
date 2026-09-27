#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Fetch the meeting-minutes graph's speech models onto an Ambarella board:
# the diarization pair (segmentation + embedding) and SenseVoice ASR.
#
#   tools/ambarella/install_meeting_models.sh
#   tools/ambarella/install_meeting_models.sh --force
#
# Diarization is not this script's to fetch. probe_diarization.py --fetch
# already pulls the segmentation bundle and the embedding model into
# ~/diarization_models, idempotently, extracting with Python's tarfile for
# the same reason this script does the same for SenseVoice below -- bzip2 is
# not on every image, and its absence reads as a corrupt download, not a
# missing package. This script shells out to that fetch mode rather than
# repeating it.
#
# SenseVoice is this script's own work, because of a wrinkle the diarization
# fetch does not have to deal with: the archive extracts into a directory of
# its own name -- verified against the segmentation bundle, which produced
# sherpa-onnx-pyannote-segmentation-3-0/. Extracting SenseVoice straight into
# ~/sensevoice would put the model one level below where the graph's
# asr_model_dir looks: meeting_transcriber globs model*.onnx in that
# directory itself, not in a subdirectory of it. So SenseVoice is downloaded
# and extracted into a staging root, and ~/sensevoice becomes a symlink to
# whichever directory the extraction actually produced -- found rather than
# hardcoded, so the archive can rename itself without silently breaking the
# graph.
#
# Idempotent: a model already in place is left alone unless --force.
#
# Roots, all overridable by environment variable:
#
#   DIAR_ROOT             where the diarization models land (default
#                          ~/diarization_models). Passed straight to
#                          probe_diarization.py --models.
#   ASR_DL_ROOT            SenseVoice's download/extraction staging area
#                          (default ~/sensevoice_dl). An implementation
#                          detail of this script; the graph never reads it.
#   SENSEVOICE_MODEL_DIR   where the graph's asr_model_dir actually looks
#                          (default ~/sensevoice). This script points it, as
#                          a symlink, at the extracted SenseVoice bundle.
#
# The graph itself reads three separate ${env:...} overrides for the
# resolved model paths -- DIARIZATION_SEG_MODEL, DIARIZATION_EMB_MODEL,
# SENSEVOICE_MODEL_DIR -- with defaults baked in for a board whose user is
# lychee. This script prints the paths it resolved at the end; copy them
# into ai_agents/.env when this board's user is someone else.
#
set -euo pipefail

DIAR_ROOT="${DIAR_ROOT:-$HOME/diarization_models}"
ASR_DL_ROOT="${ASR_DL_ROOT:-$HOME/sensevoice_dl}"
SENSEVOICE_MODEL_DIR="${SENSEVOICE_MODEL_DIR:-$HOME/sensevoice}"

# already verified reachable on 2026-09-26
ASR_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      sed -n '6,50p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

die()  { echo "FATAL: $*" >&2; exit 1; }
say()  { printf '\n\033[1m===== %s\033[0m\n' "$*"; }
ok()   { printf '    OK    %s\n' "$*"; }
info() { printf '        %s\n' "$*"; }
warn() { printf '    WARN  %s\n' "$*"; }

have() { command -v "$1" >/dev/null 2>&1; }

LOG="/tmp/install_meeting_models_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

# ------------------------------------------------------------------ 0
say "0. Preflight"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$SCRIPT_DIR/probe_diarization.py"
[[ -f "$PROBE" ]] || die "expected $PROBE next to this script"
ok "$PROBE"

for t in python3 curl; do have "$t" || die "$t is required"; done
ok "python3, curl present"
# probe_diarization.py --fetch only touches os, subprocess and tarfile --
# it does not import sherpa_onnx or numpy until the measurement step runs,
# which --fetch never reaches. Any python3 on PATH does this half; it need
# not be the interpreter the TEN runtime loads.

AVAIL_MB=$(df -Pm "$HOME" | awk 'NR==2 {print $4}')
[[ "$AVAIL_MB" -gt 2048 ]] || die "need ~2 GB free in $HOME, have ${AVAIL_MB} MB"
ok "${AVAIL_MB} MB free in $HOME"

if curl -fsS --max-time 10 -o /dev/null "https://github.com"; then
  ok "github.com reachable"
else
  die "cannot reach github.com -- download the archives elsewhere and place
  them by hand: the diarization pair under $DIAR_ROOT, following
  probe_diarization.py's own layout (run it with --fetch on a host that can
  reach github.com and copy the result across), and the SenseVoice tarball
  at $ASR_DL_ROOT/$(basename "$ASR_URL"). Then re-run this script."
fi

mkdir -p "$DIAR_ROOT" "$ASR_DL_ROOT"

# ------------------------------------------------------------------ 1
say "1. Diarization models (segmentation + embedding)"

SEG_DIR="$DIAR_ROOT/sherpa-onnx-pyannote-segmentation-3-0"
EMB_FILE="$DIAR_ROOT/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"

if [[ "$FORCE" -eq 1 ]]; then
  rm -rf "$SEG_DIR" "$EMB_FILE"
fi

python3 "$PROBE" --fetch --models "$DIAR_ROOT" || die "probe_diarization.py --fetch failed -- see the traceback above. A
  github.com you can reach but a release asset you can't (rate limiting, a
  moved release tag) is the likely cause."

[[ -f "$SEG_DIR/model.onnx" ]] || die "no $SEG_DIR/model.onnx after --fetch"
[[ -f "$EMB_FILE" ]] || die "no $EMB_FILE after --fetch"
ok "segmentation  $(du -sh "$SEG_DIR" | cut -f1)  $SEG_DIR/model.onnx"
ok "embedding     $(du -sh "$EMB_FILE" | cut -f1)  $EMB_FILE"

# ------------------------------------------------------------------ 2
say "2. SenseVoice ASR"

if [[ "$FORCE" -eq 1 ]]; then
  rm -rf "$ASR_DL_ROOT"
  mkdir -p "$ASR_DL_ROOT"
fi

find_bundle() {
  # The directory actually holding model*.onnx, whatever the archive calls
  # its own top-level directory -- found rather than hardcoded, per the
  # header comment above.
  local hit
  hit="$(find "$ASR_DL_ROOT" -maxdepth 2 -name 'model*.onnx' 2>/dev/null \
    | sort | head -1)"
  [[ -n "$hit" ]] && dirname "$hit"
}

BUNDLE="$(find_bundle || true)"

if [[ -n "$BUNDLE" ]]; then
  ok "SenseVoice already extracted at $BUNDLE"
else
  TARBALL="$ASR_DL_ROOT/$(basename "$ASR_URL")"
  if [[ ! -s "$TARBALL" ]]; then
    info "downloading $(basename "$TARBALL")"
    curl -fL -o "$TARBALL.part" "$ASR_URL" || die "download failed: $ASR_URL"
    mv "$TARBALL.part" "$TARBALL"
  fi
  ok "$(basename "$TARBALL")  $(du -h "$TARBALL" | cut -f1)"

  info "extracting with Python's tarfile, not tar xjf: bzip2 is not on" \
       "every image, and its absence reads as a corrupt download"
  python3 -c '
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:bz2") as archive:
    archive.extractall(sys.argv[2])
' "$TARBALL" "$ASR_DL_ROOT" || die "extraction failed: $TARBALL"
  rm -f "$TARBALL"

  BUNDLE="$(find_bundle || true)"
  [[ -n "$BUNDLE" ]] || die "no model*.onnx under $ASR_DL_ROOT after extracting"
fi

[[ -f "$BUNDLE/tokens.txt" ]] \
  || die "$BUNDLE has model*.onnx but no tokens.txt -- meeting_transcriber needs both"
ok "SenseVoice bundle  $(du -sh "$BUNDLE" | cut -f1)  $BUNDLE"

if [[ -e "$SENSEVOICE_MODEL_DIR" && ! -L "$SENSEVOICE_MODEL_DIR" ]]; then
  die "$SENSEVOICE_MODEL_DIR exists and is not a symlink -- move it aside, or
  set SENSEVOICE_MODEL_DIR to somewhere else, and re-run"
fi
ln -sfn "$BUNDLE" "$SENSEVOICE_MODEL_DIR"
ok "$SENSEVOICE_MODEL_DIR -> $BUNDLE"

# ------------------------------------------------------------------ 3
say "Done"
echo "  Resolved model paths. This board's graph defaults assume the user"
echo "  is lychee; if it is not, or these roots were moved, put these in"
echo "  ai_agents/.env:"
echo
printf '    DIARIZATION_SEG_MODEL=%s\n' "$SEG_DIR/model.onnx"
printf '    DIARIZATION_EMB_MODEL=%s\n' "$EMB_FILE"
printf '    SENSEVOICE_MODEL_DIR=%s\n' "$(readlink -f "$SENSEVOICE_MODEL_DIR")"
echo
echo "  Next: docs/development/board_quickstart.md, the meeting-minutes"
echo "  section -- or its Traditional Chinese twin, board_quickstart.zh-TW.md."
echo
echo "  log: $LOG"
