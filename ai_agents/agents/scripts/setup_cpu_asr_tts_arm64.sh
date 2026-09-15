#!/usr/bin/env bash
#
# Install CPU speech models on an aarch64 Ambarella board: streaming Zipformer
# ASR through sherpa-onnx, and Piper TTS.
#
# Why bother, when the board already has asr_d and tts_d: those run on the
# Vector Processor, and so does the LLM. Measured on an N1-655 on 2026-09-14,
# the VP does not run them in parallel -- every Device START in /tmp/log.txt
# waits for the previous END -- and ASR under a generating LLM went from 348 ms
# to 24 s. Moving speech to the CPU leaves the VP to the LLM alone.
#
# Follows the vendor's zipformer_piper_en_on_lychee.md. Prefers its Appendix A
# prebuilt binaries by default: building from source makes cmake fetch ONNX
# Runtime and others from GitHub, which fails on a board without that access.
# Pass --from-source to build instead.
#
# Idempotent: a component already in place is left alone unless --force.

set -euo pipefail

ASR_ROOT="${ASR_ROOT:-$HOME/zipformer_asr}"
TTS_ROOT="${TTS_ROOT:-$HOME/piper_tts}"
ENVFILE="${ENVFILE:-$HOME/.cpu_speech_env}"

SHERPA_VER="v1.12.8"
PIPER_VER="2023.11.14-2"
ORT_VER="1.16.3"
# Bilingual, because the RTC graphs run with language_hints of en and zh. The
# monolingual models are smaller but would need switching per utterance.
ASR_MODEL="sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"

FROM_SOURCE=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --from-source) FROM_SOURCE=1 ;;
    --force)       FORCE=1 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

LOG="/tmp/cpu_speech_setup_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

die()  { echo "FATAL: $*" >&2; exit 1; }
say()  { printf '\n\033[1m===== %s\033[0m\n' "$*"; }
ok()   { printf '    OK    %s\n' "$*"; }
warn() { printf '    WARN  %s\n' "$*"; }
step() { printf '  -> %s\n' "$*"; }

have() { command -v "$1" >/dev/null 2>&1; }

fetch() {
  # $1 url, $2 destination file. Skips a file that is already there.
  local url="$1" out="$2"
  if [[ -s "$out" && "$FORCE" -eq 0 ]]; then
    ok "$(basename "$out") already present"
    return
  fi
  step "downloading $(basename "$out")"
  wget -q --show-progress -O "$out.part" "$url" \
    || die "download failed: $url"
  mv "$out.part" "$out"
}

# ------------------------------------------------------------------ 0
say "0. Preflight"
[[ "$(uname -m)" == "aarch64" ]] || die "this is for the board (got $(uname -m))"
ok "aarch64"

for t in wget tar; do have "$t" || die "$t is required"; done
if [[ "$FROM_SOURCE" -eq 1 ]]; then
  for t in git cmake g++ make; do
    have "$t" || die "$t is required for --from-source"
  done
  ok "git, cmake, g++, make present"
else
  ok "wget, tar present (prebuilt mode needs no toolchain)"
fi

AVAIL_MB=$(df -Pm "$HOME" | awk 'NR==2 {print $4}')
[[ "$AVAIL_MB" -gt 2048 ]] || die "need ~2 GB free in $HOME, have ${AVAIL_MB} MB"
ok "${AVAIL_MB} MB free in $HOME"

for host in github.com huggingface.co; do
  if wget -q --spider --timeout=10 --tries=1 "https://$host" 2>/dev/null; then
    ok "$host reachable"
  else
    die "cannot reach $host -- download the archives elsewhere and place them
  in $ASR_ROOT/dl and $TTS_ROOT/dl, then re-run"
  fi
done

mkdir -p "$ASR_ROOT"/{dl,models} "$TTS_ROOT"/{dl,voices/en,voices/zh}

# ------------------------------------------------------------------ 1
say "1. ASR engine (sherpa-onnx $SHERPA_VER)"
SHERPA_BIN=""
if [[ "$FROM_SOURCE" -eq 1 ]]; then
  SRC="$ASR_ROOT/src/sherpa-onnx"
  if [[ ! -d "$SRC" || "$FORCE" -eq 1 ]]; then
    rm -rf "$SRC"; mkdir -p "$ASR_ROOT/src"
    step "cloning"
    git clone --depth 1 --branch "$SHERPA_VER" \
      https://github.com/k2-fsa/sherpa-onnx.git "$SRC" || die "clone failed"
  fi
  step "cmake + make (this pulls ONNX Runtime from GitHub, and takes a while)"
  mkdir -p "$SRC/build"
  ( cd "$SRC/build" && cmake \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=ON \
      -DSHERPA_ONNX_ENABLE_PYTHON=OFF \
      -DSHERPA_ONNX_ENABLE_TESTS=OFF \
      -DSHERPA_ONNX_ENABLE_WEBSOCKET=OFF \
      -DSHERPA_ONNX_ENABLE_TTS=OFF \
      -DSHERPA_ONNX_ENABLE_SPEAKER_DIARIZATION=OFF \
      -DSHERPA_ONNX_ENABLE_PORTAUDIO=ON \
      -DCMAKE_INSTALL_PREFIX="$ASR_ROOT/install" .. \
    && make -j"$(nproc)" && make install ) || die "sherpa-onnx build failed"
  SHERPA_HOME="$ASR_ROOT/install"
else
  TARBALL="$ASR_ROOT/dl/sherpa-onnx-$SHERPA_VER-linux-aarch64-shared-cpu.tar.bz2"
  fetch "https://github.com/k2-fsa/sherpa-onnx/releases/download/$SHERPA_VER/sherpa-onnx-$SHERPA_VER-linux-aarch64-shared-cpu.tar.bz2" "$TARBALL"
  SHERPA_HOME="$ASR_ROOT/sherpa-onnx-$SHERPA_VER-linux-aarch64-shared-cpu"
  if [[ ! -d "$SHERPA_HOME" || "$FORCE" -eq 1 ]]; then
    rm -rf "$SHERPA_HOME"
    step "extracting"
    tar xf "$TARBALL" -C "$ASR_ROOT"
  fi
fi
SHERPA_BIN="$SHERPA_HOME/bin/sherpa-onnx"
[[ -x "$SHERPA_BIN" ]] || die "no sherpa-onnx binary at $SHERPA_BIN"
file -b "$SHERPA_BIN" | grep -q 'ARM aarch64' || die "sherpa-onnx is not aarch64"
ok "$SHERPA_BIN"

# ------------------------------------------------------------------ 2
say "2. ASR model ($ASR_MODEL)"
MODEL_DIR="$ASR_ROOT/models/$ASR_MODEL"
if [[ ! -d "$MODEL_DIR" || "$FORCE" -eq 1 ]]; then
  ARCH="$ASR_ROOT/dl/$ASR_MODEL.tar.bz2"
  fetch "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$ASR_MODEL.tar.bz2" "$ARCH"
  rm -rf "$MODEL_DIR"
  step "extracting"
  tar xf "$ARCH" -C "$ASR_ROOT/models"
fi
[[ -d "$MODEL_DIR" ]] || die "model directory missing after extraction"

# The document warns that file names vary between model packages, so find them
# rather than assuming epoch-99-avg-1.
find_one() {
  local pat="$1"
  find "$MODEL_DIR" -maxdepth 1 -name "$pat" | sort | head -1
}
ENCODER="$(find_one 'encoder*.onnx')"
DECODER="$(find_one 'decoder*.onnx')"
JOINER="$(find_one 'joiner*.onnx')"
TOKENS="$MODEL_DIR/tokens.txt"
for f in "$ENCODER" "$DECODER" "$JOINER" "$TOKENS"; do
  [[ -s "$f" ]] || die "model file missing in $MODEL_DIR (encoder/decoder/joiner/tokens)"
done
ok "encoder $(basename "$ENCODER")"
ok "decoder $(basename "$DECODER")"
ok "joiner  $(basename "$JOINER")"

# ------------------------------------------------------------------ 3
say "3. TTS engine (Piper $PIPER_VER)"
if [[ "$FROM_SOURCE" -eq 1 ]]; then
  SRC="$TTS_ROOT/src/piper"
  if [[ ! -d "$SRC" || "$FORCE" -eq 1 ]]; then
    rm -rf "$SRC"; mkdir -p "$TTS_ROOT/src"
    step "cloning"
    git clone --depth 1 --branch "$PIPER_VER" \
      https://github.com/rhasspy/piper.git "$SRC" || die "clone failed"
  fi
  ORT_TGZ="$TTS_ROOT/dl/onnxruntime-linux-aarch64-$ORT_VER.tgz"
  fetch "https://github.com/microsoft/onnxruntime/releases/download/v$ORT_VER/onnxruntime-linux-aarch64-$ORT_VER.tgz" "$ORT_TGZ"
  mkdir -p "$TTS_ROOT/deps"
  [[ -d "$TTS_ROOT/deps/onnxruntime-linux-aarch64-$ORT_VER" ]] \
    || tar xf "$ORT_TGZ" -C "$TTS_ROOT/deps"
  step "staging ONNX Runtime into the source tree"
  rm -rf "$SRC/lib/Linux-aarch64"; mkdir -p "$SRC/lib/Linux-aarch64"
  cp -a "$TTS_ROOT/deps/onnxruntime-linux-aarch64-$ORT_VER/include" "$SRC/lib/Linux-aarch64/"
  cp -a "$TTS_ROOT/deps/onnxruntime-linux-aarch64-$ORT_VER/lib"     "$SRC/lib/Linux-aarch64/"
  step "cmake build"
  ( cd "$SRC" \
    && cmake -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$TTS_ROOT/install" \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build ) || die "piper build failed"
  PIPER_HOME="$TTS_ROOT/install/bin"
else
  TARBALL="$TTS_ROOT/dl/piper_linux_aarch64.tar.gz"
  fetch "https://github.com/rhasspy/piper/releases/download/$PIPER_VER/piper_linux_aarch64.tar.gz" "$TARBALL"
  PIPER_HOME="$TTS_ROOT/piper"
  if [[ ! -d "$PIPER_HOME" || "$FORCE" -eq 1 ]]; then
    rm -rf "$PIPER_HOME"
    step "extracting"
    tar xf "$TARBALL" -C "$TTS_ROOT"
  fi
fi
PIPER_BIN="$PIPER_HOME/piper"
[[ -x "$PIPER_BIN" ]] || die "no piper binary at $PIPER_BIN"
file -b "$PIPER_BIN" | grep -q 'ARM aarch64' || die "piper is not aarch64"
ok "$PIPER_BIN"

# ------------------------------------------------------------------ 4
say "4. Piper voices"
fetch "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx" \
      "$TTS_ROOT/voices/en/en_US-lessac-medium.onnx"
fetch "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json" \
      "$TTS_ROOT/voices/en/en_US-lessac-medium.onnx.json"
fetch "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx" \
      "$TTS_ROOT/voices/zh/zh_CN-huayan-medium.onnx"
fetch "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json" \
      "$TTS_ROOT/voices/zh/zh_CN-huayan-medium.onnx.json"
ok "en_US-lessac-medium and zh_CN-huayan-medium"

# ------------------------------------------------------------------ 5
say "5. Smoke test"
export LD_LIBRARY_PATH="$SHERPA_HOME/lib:$PIPER_HOME:${LD_LIBRARY_PATH:-}"

# The vendor document points at ~/asr_tts_demo/sample_16k_mono.wav, which on
# this board is one directory deeper. Look rather than assume.
WAV=""
for cand in \
  "$HOME/asr_tts_demo/app_demo/sample_16k_mono.wav" \
  "$HOME/asr_tts_demo/sample_16k_mono.wav"; do
  [[ -s "$cand" ]] && { WAV="$cand"; break; }
done

if [[ -n "$WAV" ]]; then
  step "ASR on $WAV"
  T0=$(date +%s.%N)
  ASR_OUT="$("$SHERPA_BIN" \
    --tokens="$TOKENS" --encoder="$ENCODER" \
    --decoder="$DECODER" --joiner="$JOINER" "$WAV" 2>&1)" || {
      echo "$ASR_OUT" | tail -20; die "sherpa-onnx failed"; }
  T1=$(date +%s.%N)
  printf '    elapsed %.2fs\n' "$(echo "$T1 - $T0" | bc)"
  echo "$ASR_OUT" | tail -6 | sed 's/^/      /'
else
  warn "no sample WAV found; skipping the ASR smoke test"
fi

step "TTS, English"
echo 'Hello, this is a Piper speech synthesis demo.' \
  | "$PIPER_BIN" --model "$TTS_ROOT/voices/en/en_US-lessac-medium.onnx" \
                 --output_file "$TTS_ROOT/smoke_en.wav" >/dev/null 2>&1 \
  || die "piper failed on English"
step "TTS, Chinese"
echo '歡迎使用語音合成演示。' \
  | "$PIPER_BIN" --model "$TTS_ROOT/voices/zh/zh_CN-huayan-medium.onnx" \
                 --output_file "$TTS_ROOT/smoke_zh.wav" >/dev/null 2>&1 \
  || die "piper failed on Chinese"
for f in "$TTS_ROOT/smoke_en.wav" "$TTS_ROOT/smoke_zh.wav"; do
  [[ -s "$f" ]] || die "$f is empty"
  printf '    %-34s %8s  %s\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)" \
    "$(file -b "$f" | cut -c1-60)"
done
ok "both voices synthesised"

# ------------------------------------------------------------------ 6
say "6. Environment"
cat > "$ENVFILE" <<EOF
# Written by setup_cpu_asr_tts_arm64.sh on $(date -Is).
# Source this before running anything that uses the CPU speech models.
export ASR_ROOT="$ASR_ROOT"
export TTS_ROOT="$TTS_ROOT"
export SHERPA_ONNX_BIN="$SHERPA_BIN"
export SHERPA_ONNX_MODEL_DIR="$MODEL_DIR"
export SHERPA_ONNX_TOKENS="$TOKENS"
export SHERPA_ONNX_ENCODER="$ENCODER"
export SHERPA_ONNX_DECODER="$DECODER"
export SHERPA_ONNX_JOINER="$JOINER"
export PIPER_BIN="$PIPER_BIN"
export PIPER_VOICE_EN="$TTS_ROOT/voices/en/en_US-lessac-medium.onnx"
export PIPER_VOICE_ZH="$TTS_ROOT/voices/zh/zh_CN-huayan-medium.onnx"
export LD_LIBRARY_PATH="$SHERPA_HOME/lib:$PIPER_HOME:\${LD_LIBRARY_PATH:-}"
EOF
ok "$ENVFILE"

say "Done"
echo "  Neither engine touches the Vector Processor, so the LLM keeps it."
echo
echo "  To use them:"
echo "    source $ENVFILE"
echo
echo "  Piper emits 22050 Hz mono. The RTC chain is 16 kHz throughout, so a"
echo "  TEN extension wrapping it has to resample -- ambarella_tts_python"
echo "  already does exactly that for OpenVoice, which is also 22050."
echo
echo "  log: $LOG"
