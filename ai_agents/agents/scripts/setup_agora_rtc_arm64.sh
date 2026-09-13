#!/usr/bin/env bash
#
# Take an aarch64 board from a fresh checkout to a running agora_rtc, in one
# command. Wraps the three scripts that do the real work, and adds the
# preflight and recovery that turn their failures into something diagnosable.
#
# Run this ON the aarch64 board.
#
# Usage:
#   setup_agora_rtc_arm64.sh <agora-aarch64-sdk.tgz> [example]
#
# Example:
#   setup_agora_rtc_arm64.sh \
#     ~/Agora-RTC-aarch64-linux-gnu-v4.4.32.141-20260904_143930-1281202.tgz \
#     websocket-example
#
# Everything it prints also lands in the log named at the end, so a failure can
# be pasted back whole.

set -uo pipefail

SDK_TGZ="${1:-}"
EXAMPLE="${2:-websocket-example}"

REPO="$HOME/ten-framework"
WRAPPER="$REPO/agora/agora_rtc-0.23.9-t1@dcbacc801f3"
OUT_DIR="$REPO/agora_rtc_sdk_arm64_out"
SCRIPTS="$REPO/ai_agents/agents/scripts"
LOG="/tmp/agora_rtc_arm64_setup_$(date +%Y%m%d_%H%M%S).log"

exec > >(tee -a "$LOG") 2>&1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    OK    %s\n' "$*"; }
warn() { printf '    WARN  %s\n' "$*"; }
die()  { printf '\n\033[1;31mFATAL: %s\033[0m\n\nLog: %s\n' "$*" "$LOG" >&2; exit 1; }

[[ -n "$SDK_TGZ" ]] || die "usage: $0 <agora-aarch64-sdk.tgz> [example]

The SDK tarball is Agora's aarch64 Linux build, e.g.
  ~/Agora-RTC-aarch64-linux-gnu-v4.4.32.141-20260904_143930-1281202.tgz"

# ------------------------------------------------------------------ 1. preflight
say "[1/6] Preflight"

[[ "$(uname -m)" == "aarch64" ]] \
  || die "this must run on the aarch64 board (got $(uname -m))"
ok "aarch64"

[[ -d "$REPO" ]] \
  || die "no repo at $REPO -- install_agora_rtc_arm64.sh hardcodes this path"
ok "repo at $REPO"

[[ -d "$WRAPPER" ]] || die "no wrapper source at $WRAPPER"
ok "wrapper source present"

[[ -f "$SDK_TGZ" ]] || die "no such SDK tarball: $SDK_TGZ"
ok "SDK tarball: $(basename "$SDK_TGZ")"

[[ -d "$REPO/ai_agents/agents/examples/$EXAMPLE/tenapp" ]] \
  || die "no tenapp for example '$EXAMPLE'"
ok "target example: $EXAMPLE"

# tgn ships in a submodule that a fresh clone leaves empty.
if ! command -v tgn >/dev/null 2>&1; then
  if [[ -x "$REPO/core/ten_gn/tgn" ]]; then
    export PATH="$REPO/core/ten_gn:$PATH"
    ok "tgn found in core/ten_gn, added to PATH"
  else
    say "tgn missing -- initialising the core/ten_gn submodule"
    git -C "$REPO" submodule update --init --recursive --depth 1 core/ten_gn \
      || die "could not initialise core/ten_gn"
    export PATH="$REPO/core/ten_gn:$PATH"
    command -v tgn >/dev/null 2>&1 || die "tgn still not on PATH after the submodule update"
    ok "tgn installed and on PATH"
  fi
else
  ok "tgn on PATH: $(command -v tgn)"
fi

for t in tman file tar python3 git; do
  command -v "$t" >/dev/null 2>&1 || die "$t is required but not installed"
done
ok "tman, file, tar, python3, git all present"

# ------------------------------------------------------------------ 2. recover
say "[2/6] Recovering from any interrupted previous run"

cd "$WRAPPER"

# An older version of build_agora_rtc_arm64.sh had no EXIT trap: a failed
# resolve left manifest.json stripped of its agora_rtc_sdk dependency and the
# good copy in manifest.json.bak. Put it back before anything else touches it.
if [[ -f manifest.json.bak ]]; then
  warn "manifest.json.bak found -- a previous run was interrupted"
  mv -f manifest.json.bak manifest.json
  ok "manifest.json restored from the backup"
fi

if ! python3 -c "
import json,sys
d=json.load(open('manifest.json'))
sys.exit(0 if any(x.get('name')=='agora_rtc_sdk' for x in d['dependencies']) else 1)
" 2>/dev/null; then
  warn "manifest.json is missing its agora_rtc_sdk dependency -- re-adding"
  python3 -c "
import json,collections,pathlib
p=pathlib.Path('manifest.json')
d=json.loads(p.read_text(),object_pairs_hook=collections.OrderedDict)
d['dependencies'].append(collections.OrderedDict(
    [('type','system'),('name','agora_rtc_sdk'),('version','=4.4.32-141')]))
p.write_text(json.dumps(d,indent=2,ensure_ascii=False)+chr(10))
" || die "could not repair manifest.json"
  ok "agora_rtc_sdk dependency re-added"
else
  ok "manifest.json declares agora_rtc_sdk"
fi

python3 -c "
import json
d=json.load(open('manifest.json'))
print('    deps:     ', [x.get('name') for x in d['dependencies']])
print('    supports: ', d.get('supports'))
"

# tman writes absolute symlinks into .ten/. One left behind by an install at a
# different path makes the next resolve die with EEXIST, so clear the lot --
# this is what the wrapper's own Taskfile 'clean' task removes.
STALE=0
for d in .ten out .gn .gnfiles compile_commands.json bin/agora_rtc_standalone_test; do
  if [[ -e "$d" ]]; then
    rm -rf "$d"
    STALE=1
  fi
done
if [[ "$STALE" -eq 1 ]]; then
  ok "cleared stale build artifacts (.ten, out, .gn, .gnfiles, ...)"
else
  ok "no stale artifacts"
fi

# ------------------------------------------------------------------ 3. package
say "[3/6] Repackaging the aarch64 SDK"

"$SCRIPTS/package_agora_rtc_sdk_arm64.sh" "$SDK_TGZ" "$OUT_DIR" \
  || die "SDK repackaging failed -- see the log above"

TPKG="$OUT_DIR/agora_rtc_sdk-4.4.32-141-linux-arm64.tpkg"
[[ -f "$TPKG" ]] || die "expected $TPKG, which was not produced"
ok "$(basename "$TPKG")"

# ------------------------------------------------------------------ 4. build
say "[4/6] Building the agora_rtc extension (this is the slow step)"

# 14k lines of C++ against this SDK is memory-hungry; the build script caps
# parallelism at 4, and NINJAFLAGS can lower it further on a tight board.
"$SCRIPTS/build_agora_rtc_arm64.sh" "$WRAPPER" "$TPKG" || die "the build failed.

If it ran out of memory, retry with less parallelism:
  NINJAFLAGS=\"-j 2\" $0 $SDK_TGZ $EXAMPLE"

BUILT="$WRAPPER/out/linux/arm64/ten_packages/extension/agora_rtc"
[[ -f "$BUILT/lib/libagora_rtc.so" ]] || die "libagora_rtc.so was not produced"
ok "libagora_rtc.so built"

# ------------------------------------------------------------------ 5. install
say "[5/6] Installing into $EXAMPLE"

"$SCRIPTS/install_agora_rtc_arm64.sh" "$BUILT" "$TPKG" "$EXAMPLE" \
  || die "the install failed -- see the log above"

# ------------------------------------------------------------------ 6. report
say "[6/6] Result"

TENAPP="$REPO/ai_agents/agents/examples/$EXAMPLE/tenapp"
echo "    extension: $TENAPP/ten_packages/extension/agora_rtc"
echo "    sdk:       $TENAPP/ten_packages/system/agora_rtc_sdk"
echo
echo "    graphs available in this example:"
python3 -c "
import json
d=json.load(open('$TENAPP/property.json'))
for g in d['ten']['predefined_graphs']:
    n={x['name']:x['addon'] for x in g['graph']['nodes']}
    t='agora_rtc' if 'agora_rtc' in n.values() else 'websocket_server'
    print(f\"      {g.get('name'):24} transport={t}\")
" 2>/dev/null || echo "      (could not read property.json)"

echo
say "Done"
cat <<EOF
    Next:
      1. set AGORA_APP_ID in $REPO/ai_agents/.env
         (AGORA_APP_CERTIFICATE only if your Agora project enables tokens)

      2. cd $REPO/ai_agents/agents/examples/$EXAMPLE
         task run 2>&1 | tee /tmp/task_run.log

      3. in the playground, pick the graph whose transport is agora_rtc

    RTC misconfiguration does NOT fail the graph load -- the extension starts
    either way. If nothing connects, the truth is in the log:

      grep -E "Failed to connect to Agora channel|failed to connect to channel|onConnectionFailure|onConnectionLost|onTokenExpired" /tmp/task_run.log

    Log of this run: $LOG
EOF
