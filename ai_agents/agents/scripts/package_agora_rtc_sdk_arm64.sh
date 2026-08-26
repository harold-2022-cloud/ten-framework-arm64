#!/usr/bin/env bash
#
# Repackage Agora's aarch64 Linux RTSA SDK as a TEN `agora_rtc_sdk` system
# package, so it can be installed by tman on arm64.
#
# Why this is needed: the TEN store publishes `agora_rtc_sdk` for linux/x64
# only. Agora does publish an aarch64 Linux SDK, but as a plain tarball on
# download.agora.io, in a different directory layout. This script converts one
# into the other.
#
# What this does NOT solve: the `agora_rtc` *extension* (lib/libagora_rtc.so),
# which sits on top of this SDK and implements the TEN extension interface, is
# published as a prebuilt x86-64 binary with no source. Only Agora or the TEN
# maintainers can build it for aarch64. This package is the half that can be
# prepared in advance.
#
# Usage:
#   package_agora_rtc_sdk_arm64.sh <agora-aarch64-sdk.tgz> [output-dir]
#
# Environment:
#   PKG_VERSION  version to stamp (default 4.4.32-141, to satisfy the
#                `"version": "=4.4.32-141"` exact pin in the agora_rtc
#                extension's manifest). Override if the wrapper pins another.
#   TMAN         path to the tman binary (default: tman on PATH)

set -euo pipefail

PKG_VERSION="${PKG_VERSION:-4.4.32-141}"
TMAN="${TMAN:-tman}"

die() {
  echo "FATAL: $*" >&2
  exit 1
}

[[ $# -ge 1 ]] || die "usage: $0 <agora-aarch64-sdk.tgz> [output-dir]"

SRC_TGZ="$1"
OUT_DIR="${2:-$PWD/agora_rtc_sdk_arm64_out}"

[[ -f "$SRC_TGZ" ]] || die "no such file: $SRC_TGZ"
command -v "$TMAN" >/dev/null 2>&1 || die "tman not found (set TMAN=/path/to/tman)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> Extracting $(basename "$SRC_TGZ")"
mkdir -p "$WORK/src"
tar xzf "$SRC_TGZ" -C "$WORK/src"

# Agora ships the payload under a single agora_rtc_sdk/agora_sdk directory.
SDK_ROOT="$(find "$WORK/src" -type d -name agora_sdk -print -quit)"
[[ -n "$SDK_ROOT" ]] || die "could not find an agora_sdk/ directory inside the tarball"
[[ -d "$SDK_ROOT/include" ]] || die "no include/ under $SDK_ROOT"
echo "    sdk root: ${SDK_ROOT#$WORK/src/}"

# Refuse to build a package whose libraries are not actually aarch64 -- the
# whole point is producing an arm64 package, and Agora's download page offers
# several architectures under similar names.
echo "==> Verifying architecture"
shopt -s nullglob
libs=("$SDK_ROOT"/*.so)
shopt -u nullglob
[[ ${#libs[@]} -gt 0 ]] || die "no .so files under $SDK_ROOT"
for so in "${libs[@]}"; do
  arch="$(file -b "$so")"
  case "$arch" in
  *"ARM aarch64"*) : ;;
  *) die "$(basename "$so") is not aarch64: $arch" ;;
  esac
  printf '    %-32s %s\n' "$(basename "$so")" "aarch64"
done

STAGE="$WORK/stage"
mkdir -p "$STAGE/lib" "$STAGE/include/rtc/low_level_api/include"

echo "==> Staging libraries"
cp -a "${libs[@]}" "$STAGE/lib/"

# The x64 TEN package nests headers under include/rtc/{high,low}_level_api/include.
# The aarch64 tarball is flat. The wrapper links only `createAgoraService` and
# `getAgoraSdkVersion` -- the low-level (server SDK) entry points -- so the
# aarch64 tree maps onto low_level_api. The high-level tree (IAgoraRtcEngine.h
# and friends) is genuinely absent from this SDK and is deliberately not faked.
echo "==> Staging headers into rtc/low_level_api/include"
cp -a "$SDK_ROOT/include/." "$STAGE/include/rtc/low_level_api/include/"
hdr_count="$(find "$STAGE/include" -name '*.h' | wc -l)"
echo "    $hdr_count headers"

echo "==> Writing manifest.json"
cat >"$STAGE/manifest.json" <<EOF
{
  "type": "system",
  "name": "agora_rtc_sdk",
  "version": "${PKG_VERSION}",
  "dependencies": [],
  "package": {
    "include": [
      "manifest.json",
      "PROVENANCE.md",
      "lib/**",
      "include/**"
    ]
  },
  "supports": [
    {
      "os": "linux",
      "arch": "arm64"
    }
  ]
}
EOF

# Record where this came from and how it differs from the published x64 package,
# inside the artifact itself -- a hand-assembled package with no provenance is
# a debugging trap later.
sdk_version="$(strings "$SDK_ROOT/libagora_rtc_sdk.so" 2>/dev/null |
  grep -oE '^4\.[0-9]+\.[0-9]+([.-][0-9A-Za-z]+)?' | sort -u | head -1 || true)"

echo "==> Writing PROVENANCE.md"
cat >"$STAGE/PROVENANCE.md" <<EOF
# agora_rtc_sdk (linux/arm64) — provenance

Not an official TEN store package. Repackaged from Agora's aarch64 Linux SDK
tarball by \`ai_agents/agents/scripts/package_agora_rtc_sdk_arm64.sh\`.

| | |
| --- | --- |
| Source tarball | \`$(basename "$SRC_TGZ")\` |
| SDK version string in libagora_rtc_sdk.so | \`${sdk_version:-unknown}\` |
| Version stamped on this package | \`${PKG_VERSION}\` |
| Target | linux/arm64 |

## The version stamp is deliberate and is not the upstream build number

The \`agora_rtc\` extension pins \`"version": "=4.4.32-141"\` exactly, so a
package that does not carry that version will not resolve as its dependency.
The aarch64 tarball reports \`${sdk_version:-unknown}\` instead. Same minor
version, different build. If a symbol or behaviour difference is ever suspected,
this mismatch is the first thing to check.

## Libraries included

$(cd "$STAGE/lib" && for f in *.so; do echo "- \`$f\`"; done)

## Libraries the published x64 package has and this one does not

The x64 package ships eight libraries. The aarch64 tarball ships the three
above. Missing here:

- \`libagora-ffmpeg.so\` — listed as \`NEEDED\` by the x64 \`libagora_rtc.so\`
- \`libagora-soundtouch.so\` — listed as \`NEEDED\` by the x64 \`libagora_rtc.so\`
- \`libagora_stt_ag_extension.so\`
- \`libagora_stt_ms_extension.so\`
- \`libagora_mcc_ysd_extension.so\`

The first two matter: if an aarch64 \`libagora_rtc.so\` is built with the same
link line as the x64 one, it will fail to load against this package. Either
those libraries need to be in the aarch64 SDK bundle, or the aarch64 wrapper
must be linked without them.

## Header layout

Headers are placed under \`include/rtc/low_level_api/include/\`, matching where
the x64 package puts \`IAgoraService.h\`, \`AgoraBase.h\` and the \`NGIAgora*.h\`
family.

\`include/rtc/high_level_api/include/\` is **not** populated. The aarch64 tarball
is the RTSA / server SDK and does not ship the high-level client API
(\`IAgoraRtcEngine.h\`, \`IAgoraRtcEngineEx.h\`, \`IAgoraMediaEngine.h\`,
\`IAgoraMusicContentCenter.h\`, \`IAgoraSpatialAudio.h\` and 40 internal
\`*_i.h\` headers). That is not a defect in this package: the \`agora_rtc\`
wrapper resolves only \`createAgoraService\` and \`getAgoraSdkVersion\` from the
SDK and references no \`agora::\` class symbols directly, so it uses the
low-level API exclusively.
EOF

echo "==> Running tman package"
mkdir -p "$OUT_DIR"
(cd "$STAGE" && "$TMAN" package --output-path "$OUT_DIR/agora_rtc_sdk-${PKG_VERSION}-linux-arm64.tpkg")

TPKG="$OUT_DIR/agora_rtc_sdk-${PKG_VERSION}-linux-arm64.tpkg"
[[ -f "$TPKG" ]] || die "tman package did not produce $TPKG"

echo
echo "==> Done"
ls -lh "$TPKG"
echo
echo "Contents:"
tar tzf "$TPKG" | head -12
echo "  ... $(tar tzf "$TPKG" | wc -l) entries total"
echo
echo "Install into a tenapp with:"
echo "  tman install --os linux --arch arm64   # after publishing to a registry"
echo "or point tman at a local registry; see docs/development/arm64_build.md"
