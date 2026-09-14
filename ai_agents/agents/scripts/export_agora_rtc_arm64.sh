#!/usr/bin/env bash
#
# Copy the built aarch64 agora_rtc into the repository so it can be committed,
# and others can install it without a toolchain or an SDK download.
#
# Only what the runtime needs is carried: the extension's own library and the
# five SDK libraries it links against. The SDK's 154 headers are build-time
# only -- nothing reads them at run time -- and leaving them out is most of
# the size.
#
# Run this on the board, then commit and push from there. That avoids moving
# 20-odd MB of binaries between machines by hand.

set -euo pipefail

REPO="${REPO:-$HOME/ten-framework}"
WRAPPER="${WRAPPER:-$REPO/agora/agora_rtc-0.23.9-t1@dcbacc801f3}"
BUILT="$WRAPPER/out/linux/arm64/ten_packages/extension/agora_rtc"
SDK_TPKG="${SDK_TPKG:-$REPO/agora_rtc_sdk_arm64_out/agora_rtc_sdk-4.4.32-141-linux-arm64.tpkg}"
DEST="$REPO/ai_agents/agents/prebuilt/linux-arm64"

# The official x64 wrapper lists exactly these five as NEEDED. The three ASR
# plugins in the SDK package are not linked and enable_agora_asr is false.
SDK_LIBS=(libagora_rtc_sdk libaosl libagora-fdkaac libagora-ffmpeg libagora-soundtouch)

die() { echo "FATAL: $*" >&2; exit 1; }
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()  { printf '    OK  %s\n' "$*"; }

say "1. Checking the build output"
[[ "$(uname -m)" == "aarch64" ]] || die "run this on the board (got $(uname -m))"
[[ -f "$BUILT/lib/libagora_rtc.so" ]] || die "no build at $BUILT -- run setup_agora_rtc_arm64.sh first"
[[ -f "$SDK_TPKG" ]] || die "no SDK package at $SDK_TPKG"
file -b "$BUILT/lib/libagora_rtc.so" | grep -q 'ARM aarch64' \
  || die "libagora_rtc.so is not aarch64"
ok "libagora_rtc.so is aarch64"

say "2. Staging the extension"
rm -rf "$DEST/agora_rtc"
mkdir -p "$DEST/agora_rtc/lib"
cp "$BUILT/manifest.json" "$BUILT/property.json" "$DEST/agora_rtc/"
cp "$BUILT/lib/libagora_rtc.so" "$DEST/agora_rtc/lib/"
# The build strips the SDK dependency to let tman resolve, and restores it
# afterwards. Carrying a manifest without it would install a package the
# runtime cannot satisfy.
python3 -c "
import json, sys
d = json.load(open('$DEST/agora_rtc/manifest.json'))
names = [x.get('name') for x in d.get('dependencies', [])]
sys.exit(0 if 'agora_rtc_sdk' in names else 1)
" || die "the staged manifest.json is missing its agora_rtc_sdk dependency"
ok "manifest.json declares agora_rtc_sdk"

say "3. Staging the SDK libraries"
rm -rf "$DEST/agora_rtc_sdk"
mkdir -p "$DEST/agora_rtc_sdk/lib"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar xzf "$SDK_TPKG" -C "$TMP"
cp "$TMP/manifest.json" "$DEST/agora_rtc_sdk/"
for lib in "${SDK_LIBS[@]}"; do
  src="$TMP/lib/${lib}.so"
  [[ -f "$src" ]] || die "$lib.so missing from $SDK_TPKG"
  file -b "$src" | grep -q 'ARM aarch64' || die "$lib.so is not aarch64"
  cp "$src" "$DEST/agora_rtc_sdk/lib/"
done
ok "${#SDK_LIBS[@]} libraries, all aarch64"

say "4. Symbols"
# tgn's release build is not stripped, and the symbol table is most of the
# size. Stripping costs readable backtraces from a crash inside the wrapper,
# which is why it is opt-in rather than automatic.
for f in "$DEST/agora_rtc/lib/libagora_rtc.so" "$DEST"/agora_rtc_sdk/lib/*.so; do
  printf '    %-28s %6s  %s\n' "$(basename "$f")" \
    "$(du -h "$f" | cut -f1)" \
    "$(file -b "$f" | grep -o 'not stripped\|stripped' | head -1)"
done
if [[ "${STRIP:-0}" == "1" ]]; then
  echo
  find "$DEST" -name '*.so' -exec strip --strip-unneeded {} +
  echo "    stripped:"
  for f in "$DEST/agora_rtc/lib/libagora_rtc.so" "$DEST"/agora_rtc_sdk/lib/*.so; do
    printf '    %-28s %6s\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
  done
else
  echo
  echo "    re-run with STRIP=1 to drop the symbol tables"
fi

say "5. Result"
find "$DEST" -type f | sed "s|$DEST/|    |" | sort
echo
du -sh "$DEST"
echo
WRONG=$(find "$DEST" -name '*.so' -exec file -b {} \; | grep -c 'x86-64' || true)
[[ "$WRONG" -eq 0 ]] || die "$WRONG x86-64 object(s) staged"
ok "no x86-64 objects"

say "6. Next"
echo "  .gitignore carries an exception for this path, so no -f is needed:"
echo
echo "    cd $REPO"
echo "    git add ai_agents/agents/prebuilt/linux-arm64"
echo "    git commit -m 'feat(agora-rtc): ship the prebuilt aarch64 extension'"
echo "    git push arm64 feat/arm64-native-build"
