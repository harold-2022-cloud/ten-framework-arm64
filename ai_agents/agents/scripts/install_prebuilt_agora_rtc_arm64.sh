#!/usr/bin/env bash
#
# Install the prebuilt aarch64 agora_rtc into an example's tenapp.
#
# For anyone who just wants to run it: no toolchain, no SDK download, no
# 14000-line C++ build. The artifacts come from ai_agents/agents/prebuilt,
# put there by export_agora_rtc_arm64.sh on a machine that did build them.
#
# To build from source instead, see setup_agora_rtc_arm64.sh.

set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
SRC="$REPO/ai_agents/agents/prebuilt/linux-arm64"
EXAMPLE="${1:-voice-assistant}"
TENAPP="$REPO/ai_agents/agents/examples/$EXAMPLE/tenapp"

SDK_LIBS=(libagora_rtc_sdk libaosl libagora-fdkaac libagora-ffmpeg libagora-soundtouch)

die() { echo "FATAL: $*" >&2; exit 1; }
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()  { printf '    OK  %s\n' "$*"; }

say "1. Preflight"
[[ "$(uname -m)" == "aarch64" ]] \
  || die "these are aarch64 binaries and this is $(uname -m)"
[[ -d "$SRC" ]] || die "no prebuilt packages at $SRC"
[[ -d "$TENAPP" ]] || die "no tenapp for example '$EXAMPLE'"
ok "aarch64, prebuilt present, target example $EXAMPLE"

say "2. Placing the extension"
rm -rf "$TENAPP/ten_packages/extension/agora_rtc"
mkdir -p "$TENAPP/ten_packages/extension"
cp -r "$SRC/agora_rtc" "$TENAPP/ten_packages/extension/agora_rtc"
ok "$(find "$TENAPP/ten_packages/extension/agora_rtc" -type f | wc -l) files"

say "3. Placing the SDK"
rm -rf "$TENAPP/ten_packages/system/agora_rtc_sdk"
mkdir -p "$TENAPP/ten_packages/system"
cp -r "$SRC/agora_rtc_sdk" "$TENAPP/ten_packages/system/agora_rtc_sdk"
for lib in "${SDK_LIBS[@]}"; do
  [[ -f "$TENAPP/ten_packages/system/agora_rtc_sdk/lib/${lib}.so" ]] \
    || die "$lib.so missing from the prebuilt package"
done
ok "${#SDK_LIBS[@]} libraries in place"

say "4. Verifying architecture"
# The runtime dlopens every .so under an addon's lib/, so one object of the
# wrong architecture fails the load rather than being skipped.
WRONG=$(find "$TENAPP/ten_packages" -name '*.so' -exec file -b {} \; \
        | grep -c 'x86-64' || true)
RIGHT=$(find "$TENAPP/ten_packages" -name '*.so' -exec file -b {} \; \
        | grep -c 'ARM aarch64' || true)
echo "    aarch64: $RIGHT    x86-64: $WRONG"
[[ "$WRONG" -eq 0 ]] || die "an x86-64 object under lib/ fails dlopen"
ok "no x86-64 objects"

say "5. Verifying symbol resolution"
cd "$TENAPP"
UNRESOLVED=$(
  LD_LIBRARY_PATH="ten_packages/system/ten_runtime/lib:ten_packages/system/agora_rtc_sdk/lib" \
  ldd -r ten_packages/extension/agora_rtc/lib/libagora_rtc.so 2>&1 \
    | grep -E 'not found|undefined symbol' || true
)
if [[ -n "$UNRESOLVED" ]]; then
  echo "$UNRESOLVED" | sed -n '1,20p' | sed 's/^/      /'
  die "unresolved symbols -- run task install first so ten_runtime is present"
fi
ok "every symbol resolves"

say "Installed into $TENAPP"
echo
echo "    Next:"
echo "      1. set AGORA_APP_ID in $REPO/ai_agents/.env"
echo "      2. cd $REPO/ai_agents/agents/examples/$EXAMPLE && task run"
echo "      3. pick a graph whose transport is agora_rtc"
