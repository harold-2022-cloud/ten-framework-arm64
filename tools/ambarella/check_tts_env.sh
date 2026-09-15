#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Report what is actually installed on the board before running the
# sherpa_onnx_tts_python tests or its graph.
#
# Reports only. It installs nothing, changes nothing, and always exits 0 so a
# missing piece does not cut the report short.
#
#   tools/ambarella/check_tts_env.sh
#
# The board runs a vendor-patched Fedora with no backup, so this also reports
# which Python packages in the system path are owned by rpm: those are the
# ones a `pip install --system` would overwrite.

set -u

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
EXT="$REPO_ROOT/ai_agents/agents/ten_packages/extension/sherpa_onnx_tts_python"

section() {
  printf '\n== %s ==\n' "$1"
}

# ---------------------------------------------------------------- repository

section "repository"
echo "root      $REPO_ROOT"
git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null |
  sed 's/^/branch    /'
git -C "$REPO_ROOT" log --oneline -1 2>/dev/null | sed 's/^/commit    /'
if [[ -d "$EXT" ]]; then
  echo "extension present"
else
  echo "extension MISSING -- the pull did not land"
fi

# -------------------------------------------------------------------- python

section "python"
command -v python3 | sed 's/^/python3   /'
python3 -V 2>&1 | sed 's/^/version   /'
python3 -c 'import site,sys
print("system    " + (site.getsitepackages()[0] if hasattr(site,"getsitepackages") else "?"))
print("user      " + site.getusersitepackages())' 2>/dev/null

section "python packages"
# Where each one resolves from matters as much as whether it imports: a
# package under the user path is one pip put there, a package under the
# system path may be one rpm owns.
python3 - <<'PY' 2>&1
import importlib, site, sys

try:
    system_dirs = tuple(site.getsitepackages())
except AttributeError:
    system_dirs = ()
user_dir = site.getusersitepackages()

for name in (
    "sherpa_onnx", "numpy", "scipy", "pydantic", "pytest", "pytest_asyncio"
):
    try:
        mod = importlib.import_module(name)
    except Exception as err:
        print(f"  {name:16} MISSING ({type(err).__name__}: {err})")
        continue
    version = getattr(mod, "__version__", "?")
    path = getattr(mod, "__file__", "") or ""
    where = "other"
    if path.startswith(user_dir):
        where = "user"
    elif any(path.startswith(d) for d in system_dirs):
        where = "system"
    print(f"  {name:16} {version:12} {where:7} {path}")
PY

section "rpm ownership of the system python path"
# A pip install into the system path would overwrite these.
if command -v rpm >/dev/null 2>&1; then
  for name in numpy scipy pydantic; do
    file=$(python3 -c "
import importlib
try:
    print(importlib.import_module('$name').__file__ or '')
except Exception:
    print('')
" 2>/dev/null)
    if [[ -z "$file" ]]; then
      echo "  $name: not installed"
      continue
    fi
    owner=$(rpm -qf "$file" 2>&1)
    echo "  $name: $owner"
  done
else
  echo "  rpm not on PATH"
fi

# ---------------------------------------------------------- ten dependencies

section "ten_ai_base and ten_runtime_python"
# tests/bin/start needs both. They are produced by `task install`, not by the
# checkout, so their absence means the example has not been installed here.
found=0
while IFS= read -r dir; do
  echo "  $dir"
  found=1
done < <(find "$REPO_ROOT" -type d \
  \( -name ten_ai_base -o -name ten_runtime_python \) 2>/dev/null | sort)
if [[ $found -eq 0 ]]; then
  echo "  none -- run task install in an example first"
fi

section "TEN_SYSTEM_DIR candidates"
# What tests/bin/start should be pointed at: a directory holding both.
found=0
while IFS= read -r dir; do
  if [[ -d "$dir/ten_ai_base" && -d "$dir/ten_runtime_python" ]]; then
    echo "  $dir"
    found=1
  fi
done < <(find "$REPO_ROOT" -type d -name system 2>/dev/null | sort)
if [[ $found -eq 0 ]]; then
  echo "  none"
fi

# --------------------------------------------------------------------- voice

section "voices"
for root in "$HOME/piper_tts/vits" "$HOME/piper_tts"; do
  [[ -d "$root" ]] || continue
  echo "  $root"
  for voice in "$root"/*/; do
    [[ -d "$voice" ]] || continue
    name=$(basename "$voice")
    onnx=$(find "$voice" -maxdepth 1 -name '*.onnx' 2>/dev/null | wc -l)
    tokens="no"
    [[ -f "$voice/tokens.txt" ]] && tokens="yes"
    espeak="no"
    [[ -d "$voice/espeak-ng-data" ]] && espeak="yes"
    size=$(du -sh "$voice" 2>/dev/null | cut -f1)
    echo "    $name  onnx=$onnx tokens=$tokens espeak-ng-data=$espeak size=$size"
  done
  break
done
[[ -d "$HOME/piper_tts" ]] || echo "  no ~/piper_tts"

# ------------------------------------------------------------------ toolchain

section "toolchain"
for tool in task tman go bun uv pip pip3; do
  path=$(command -v "$tool" 2>/dev/null)
  printf '  %-6s %s\n' "$tool" "${path:-MISSING}"
done

section "vector processor"
# The point of this extension is not to need it. Reported so a later
# comparison has a before.
if [[ -e /dev/cavalry ]]; then
  echo "  /dev/cavalry present"
  if command -v fuser >/dev/null 2>&1; then
    fuser -v /dev/cavalry 2>&1 | sed 's/^/  /'
  else
    echo "  fuser not on PATH, cannot list holders"
  fi
else
  echo "  /dev/cavalry absent"
fi

printf '\n== done ==\n'
exit 0
