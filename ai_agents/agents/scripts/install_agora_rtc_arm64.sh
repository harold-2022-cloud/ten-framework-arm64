#!/usr/bin/env bash
#
# Install the locally built arm64 agora_rtc into an example's tenapp.
#
# The registry has no arm64 build of agora_rtc, so tman cannot resolve it. This
# installs everything else through tman, then places the built extension and the
# repackaged SDK by hand.
#
# Layout verified against an official x64 `tman install`:
#
#   ten_packages/extension/agora_rtc/{manifest.json,property.json,lib/*.so}
#   ten_packages/system/agora_rtc_sdk/{include,lib}/
#
# Usage:
#   install_agora_rtc_arm64.sh <built-extension-dir> <sdk.tpkg> [example]
#
# Example:
#   install_agora_rtc_arm64.sh \
#     ~/agora_rtc-0.23.9-t1@dcbacc801f3/out/linux/arm64/ten_packages/extension/agora_rtc \
#     ~/agora_rtc_sdk-4.4.32-141-linux-arm64.tpkg \
#     voice-assistant
#
set -euo pipefail

BUILT="${1:-}"
SDK_TPKG="${2:-}"
EXAMPLE="${3:-voice-assistant}"
TENAPP="$HOME/ten-framework/ai_agents/agents/examples/$EXAMPLE/tenapp"

die() { echo "FATAL: $*" >&2; exit 1; }
ok()  { echo "    OK  $*"; }

[[ -n "$BUILT" && -d "$BUILT" ]] || die "usage: $0 <built-extension-dir> <sdk.tpkg> [example]"
[[ -n "$SDK_TPKG" && -f "$SDK_TPKG" ]] || die "usage: $0 <built-extension-dir> <sdk.tpkg> [example]"
[[ -d "$TENAPP" ]] || die "no tenapp at $TENAPP"

BUILT="$(cd "$BUILT" && pwd)"
SDK_TPKG="$(cd "$(dirname "$SDK_TPKG")" && pwd)/$(basename "$SDK_TPKG")"

# ---------------------------------------------------------------- 1
echo "==> [1/6] Checking the build output"
for f in manifest.json property.json; do
  [[ -f "$BUILT/$f" ]] || die "$BUILT/$f missing"
done
[[ -f "$BUILT/lib/libagora_rtc.so" ]] || die "$BUILT/lib/libagora_rtc.so missing"
file -b "$BUILT/lib/libagora_rtc.so" | grep -q 'ARM aarch64' \
  || die "libagora_rtc.so is not aarch64"
ok "manifest.json, property.json, lib/libagora_rtc.so (aarch64)"

# ---------------------------------------------------------------- 2
echo "==> [2/6] Installing the other dependencies"
cd "$TENAPP"

# tman resolves the whole dependency tree or fails, and the registry has no
# arm64 agora_rtc. Drop it for the resolve so the ~40 Python extensions install,
# then restore the manifest untouched.
if [[ -f manifest.json.bak ]]; then
  die "manifest.json.bak exists from an interrupted run.
  Its manifest.json is the edited one, missing the agora_rtc dependency.
  Restore it first:
    mv $TENAPP/manifest.json.bak $TENAPP/manifest.json"
fi

cp manifest.json manifest.json.bak

# Restore on ANY exit, including a failed resolve -- otherwise the tenapp is
# left permanently missing its agora_rtc dependency.
restore_manifest() {
  if [[ -f "$TENAPP/manifest.json.bak" ]]; then
    mv -f "$TENAPP/manifest.json.bak" "$TENAPP/manifest.json"
    echo "    OK  manifest restored"
  fi
}
trap restore_manifest EXIT
python3 - <<'PY'
import json, collections, pathlib
p = pathlib.Path("manifest.json")
d = json.loads(p.read_text(), object_pairs_hook=collections.OrderedDict)
before = len(d["dependencies"])
d["dependencies"] = [x for x in d["dependencies"] if x.get("name") != "agora_rtc"]
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"    dropped agora_rtc for this resolve ({before} -> {len(d['dependencies'])} deps)")
PY

tman -y install

restore_manifest
trap - EXIT

# ---------------------------------------------------------------- 3
echo "==> [3/6] Placing the extension"
rm -rf "$TENAPP/ten_packages/extension/agora_rtc"
mkdir -p "$TENAPP/ten_packages/extension"
cp -r "$BUILT" "$TENAPP/ten_packages/extension/agora_rtc"
ok "$(find "$TENAPP/ten_packages/extension/agora_rtc" -type f | wc -l) files"

# ---------------------------------------------------------------- 4
echo "==> [4/6] Placing the SDK"
rm -rf "$TENAPP/ten_packages/system/agora_rtc_sdk"
mkdir -p "$TENAPP/ten_packages/system/agora_rtc_sdk"
tar xzf "$SDK_TPKG" -C "$TENAPP/ten_packages/system/agora_rtc_sdk"

# The official x64 wrapper lists exactly these five as NEEDED; the three further
# libraries in the x64 SDK package (mcc_ysd, stt_ag, stt_ms) are not linked.
for lib in libagora_rtc_sdk libaosl libagora-fdkaac libagora-ffmpeg libagora-soundtouch; do
  f="$TENAPP/ten_packages/system/agora_rtc_sdk/lib/${lib}.so"
  [[ -f "$f" ]] || die "$lib.so missing from the SDK package"
done
ok "5 libraries in place"

# ---------------------------------------------------------------- 5
echo "==> [5/6] Verifying architecture"
WRONG=$(find "$TENAPP/ten_packages" -name '*.so' -exec file -b {} \; | grep -c 'x86-64' || true)
RIGHT=$(find "$TENAPP/ten_packages" -name '*.so' -exec file -b {} \; | grep -c 'ARM aarch64' || true)
echo "    aarch64: $RIGHT    x86-64: $WRONG"
if [[ "$WRONG" -ne 0 ]]; then
  echo "    the following are the wrong architecture:"
  find "$TENAPP/ten_packages" -name '*.so' \
    -exec sh -c 'file -b "$1" | grep -q x86-64 && echo "      $1"' _ {} \;
  die "an x86-64 object under lib/ fails dlopen; the runtime loads every .so it finds there"
fi
ok "no x86-64 objects"

# ---------------------------------------------------------------- 6
echo "==> [6/6] Verifying symbol resolution"
# Resolve against the same directories the runtime will use. Without this, every
# symbol from libten_runtime and libagora_rtc_sdk reports as undefined, which
# looks alarming and means nothing.
cd "$TENAPP"
UNRESOLVED=$(
  LD_LIBRARY_PATH="ten_packages/system/ten_runtime/lib:ten_packages/system/agora_rtc_sdk/lib" \
  ldd -r ten_packages/extension/agora_rtc/lib/libagora_rtc.so 2>&1 \
    | grep -E 'not found|undefined symbol' || true
)
if [[ -n "$UNRESOLVED" ]]; then
  echo "$UNRESOLVED" | sed 's/^/      /'
  die "unresolved symbols"
fi
ok "every symbol resolves"

echo
echo "==> Installed into $TENAPP"
echo
echo "    Next:"
echo "      1. set AGORA_APP_ID in ~/ten-framework/ai_agents/.env"
echo "      2. tmux new -d -s ten 'cd ~/ten-framework/ai_agents/agents/examples/$EXAMPLE && task run 2>&1 | tee /tmp/task_run.log'"
