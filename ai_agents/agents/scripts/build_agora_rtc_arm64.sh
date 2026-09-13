#!/usr/bin/env bash
#
# Build the agora_rtc extension for aarch64.
#
# Run this ON the aarch64 machine. There is no cross-compilation setup in this
# repo -- CI only ever builds arm64 on native arm64 runners.
#
# Expects, in the current directory:
#   agora_rtc-master@<sha>/                     the wrapper source
#   agora_rtc_sdk-4.4.32-141-linux-arm64.tpkg   the packaged SDK
#
set -euo pipefail

WRAPPER_DIR="${1:-}"
SDK_TPKG="${2:-}"

die() { echo "FATAL: $*" >&2; exit 1; }

[[ -n "$WRAPPER_DIR" && -d "$WRAPPER_DIR" ]] || die "usage: $0 <wrapper-src-dir> <sdk.tpkg>"
[[ -n "$SDK_TPKG" && -f "$SDK_TPKG" ]] || die "usage: $0 <wrapper-src-dir> <sdk.tpkg>"

[[ "$(uname -m)" == "aarch64" ]] || die "this must run on aarch64 (got $(uname -m))"

WRAPPER_DIR="$(cd "$WRAPPER_DIR" && pwd)"
SDK_TPKG="$(cd "$(dirname "$SDK_TPKG")" && pwd)/$(basename "$SDK_TPKG")"

# ---------------------------------------------------------------- 1. toolchain
echo "==> [1/6] Toolchain"

# The wrapper links z, crypto and ssl from the system, on top of the Agora
# libraries. See its BUILD.gn.
sudo dnf -y install openssl-devel zlib-devel gcc gcc-c++ make cmake git

command -v tgn >/dev/null 2>&1 || die "tgn not on PATH.
  cd ~/ten-framework
  git submodule update --init --recursive --depth 1 core/ten_gn
  export PATH=\$HOME/ten-framework/core/ten_gn:\$PATH"

command -v tman >/dev/null 2>&1 || die "tman not on PATH"

tgn -h >/dev/null 2>&1 || die "tgn is not runnable"
echo "    tgn and tman present"

# ---------------------------------------------------------------- 2. deps
echo "==> [2/6] Installing dependencies except the SDK"

cd "$WRAPPER_DIR"

# tman consults only the registry named "default", so a local file:// registry
# cannot coexist with the official one. The SDK is therefore installed by hand
# below, and its dependency is taken out of the manifest for the duration of
# the resolve so the rest can install.
# A leftover .bak means a previous run died between the edit and the restore.
# Copying over it now would destroy the only good copy, so refuse instead.
if [[ -f manifest.json.bak ]]; then
  die "manifest.json.bak exists from an interrupted run.
  Its manifest.json is the edited one, missing the agora_rtc_sdk dependency.
  Restore it first:
    mv $WRAPPER_DIR/manifest.json.bak $WRAPPER_DIR/manifest.json"
fi

cp manifest.json manifest.json.bak

# Restore on ANY exit, including a failed resolve. Without this the script
# leaves manifest.json permanently missing the agora_rtc_sdk dependency, and
# the next run copies that damaged file over the backup.
restore_manifest() {
  if [[ -f "$WRAPPER_DIR/manifest.json.bak" ]]; then
    mv -f "$WRAPPER_DIR/manifest.json.bak" "$WRAPPER_DIR/manifest.json"
    echo "    manifest restored"
  fi
}
trap restore_manifest EXIT
python3 - <<'PY'
import json, collections, pathlib
p = pathlib.Path("manifest.json")
d = json.loads(p.read_text(), object_pairs_hook=collections.OrderedDict)
d["dependencies"] = [x for x in d.get("dependencies", [])
                     if x.get("name") != "agora_rtc_sdk"]
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print("    agora_rtc_sdk dependency removed for this resolve")
PY

tman -y install --standalone

restore_manifest
trap - EXIT

# ---------------------------------------------------------------- 3. SDK
echo "==> [3/6] Unpacking the SDK into the standalone app"

SYS_DIR="$WRAPPER_DIR/.ten/app/ten_packages/system"
[[ -d "$SYS_DIR" ]] || die "$SYS_DIR missing -- did tman install succeed?"

rm -rf "$SYS_DIR/agora_rtc_sdk"
mkdir -p "$SYS_DIR/agora_rtc_sdk"
tar xzf "$SDK_TPKG" -C "$SYS_DIR/agora_rtc_sdk"

for lib in libagora_rtc_sdk libaosl libagora-fdkaac libagora-ffmpeg libagora-soundtouch; do
  f="$SYS_DIR/agora_rtc_sdk/lib/${lib}.so"
  [[ -f "$f" ]] || die "missing $lib.so in the package"
  file -b "$f" | grep -q 'ARM aarch64' || die "$lib.so is not aarch64"
done
echo "    5 libraries in place, all aarch64"

[[ -f "$SYS_DIR/agora_rtc_sdk/include/rtc/low_level_api/include/IAgoraService.h" ]] \
  || die "header layout is wrong -- IAgoraService.h not where BUILD.gn expects it"
echo "    headers where BUILD.gn expects them"

# ---------------------------------------------------------------- 4. generate
echo "==> [4/6] tgn gen"

# is_clang=false matches what the wrapper's own Taskfile uses. arm64 replaces
# x64 -- the only difference from the official build.
tgn gen linux arm64 release -- is_clang=false

# ---------------------------------------------------------------- 5. build
echo "==> [5/6] tgn build"

# Cap parallelism the way the wrapper's Taskfile allows; 16k lines of C++
# against this SDK is memory-hungry.
export NINJAFLAGS="${NINJAFLAGS:--j $(( $(nproc) > 4 ? 4 : $(nproc) ))}"
tgn build linux arm64 release

# ---------------------------------------------------------------- 6. verify
echo "==> [6/6] Verifying"

OUT="$WRAPPER_DIR/out/linux/arm64/ten_packages/extension/agora_rtc"
[[ -f "$OUT/lib/libagora_rtc.so" ]] || die "libagora_rtc.so was not produced"

echo -n "    arch: "
file -b "$OUT/lib/libagora_rtc.so" | grep -oE 'ARM aarch64|x86-64' \
  || die "unexpected architecture: $(file -b "$OUT/lib/libagora_rtc.so")"

echo "    NEEDED:"
readelf -d "$OUT/lib/libagora_rtc.so" | awk '/NEEDED/ {print "      " $NF}'

echo "    unresolved symbols against the SDK:"
# Collect once, then slice with sed. Piping grep into head made grep die of
# SIGPIPE past the tenth symbol, which killed the script through pipefail --
# precisely when the list this prints is the thing worth reading.
UNRESOLVED="$(ldd -r "$OUT/lib/libagora_rtc.so" 2>&1 | grep 'undefined symbol' || true)"
if [[ -n "$UNRESOLVED" ]]; then
  echo "$UNRESOLVED" | sed -n '1,20p' | sed 's/^/      /'
  echo "      ... $(echo "$UNRESOLVED" | wc -l) total"
else
  echo "      none"
fi

echo
echo "==> Built: $OUT/lib/libagora_rtc.so"
