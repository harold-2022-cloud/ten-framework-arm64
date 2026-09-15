#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Put sherpa_onnx_tts_python's dependencies where the TEN runtime can import
# them -- which is not necessarily where `pip install` puts them.
#
#   tools/ambarella/setup_tts_runtime_deps.sh            # check, then install
#   tools/ambarella/setup_tts_runtime_deps.sh --check    # report only
#   tools/ambarella/setup_tts_runtime_deps.sh --yes      # install even if the
#                                                        # plan cannot be shown
#   tools/ambarella/setup_tts_runtime_deps.sh --fix-split # copy native libraries
#                                                        # that landed in the
#                                                        # wrong site directory
#
# The runtime embeds one interpreter and the shell has another. On the arm64
# board the runtime loads libpython3.12 while `python3` is 3.13, and the
# runtime's sys.path carries no user-site directory at all, so a
# `pip install --user` lands somewhere it will never look. The graph then
# loads every node except the one whose dependencies are "installed".
#
# Each step states what it found before acting, and stops at the first one
# that cannot be satisfied. Nothing is installed without printing the plan.

set -uo pipefail

CHECK_ONLY=0
ASSUME_YES=0
FIX_SPLIT=0
for arg in "$@"; do
  case "$arg" in
    --check|--dry-run) CHECK_ONLY=1 ;;
    --yes) ASSUME_YES=1 ;;
    --fix-split) FIX_SPLIT=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# Set by step 3. Declared here because the diagnosis can run before it, from
# step 2, when a package is installed but unusable.
SUDO=()
INSTALLER=(pip install)
TASK_LOG="${TASK_LOG:-/tmp/task_run.log}"
PACKAGES=(numpy scipy sherpa_onnx)
# Import name -> what to install it by.
declare -A DIST=([numpy]="numpy>=1.24.0" [scipy]="scipy" [sherpa_onnx]="sherpa-onnx>=1.13.8")

# An ImportError that names a shared object is not a missing package: the
# package is installed and its native library is somewhere the loader will not
# look. The wheels rely on RPATH $ORIGIN, so the two have to sit in the same
# directory -- and a distribution split across lib and lib64, as Fedora splits
# them, puts them in different ones.
diagnose_missing_object() {
  local module="$1" message="$2"
  local object
  object=$(echo "$message" | sed -n 's/.*ImportError: \([^:]*\.so[^:]*\): cannot open.*/\1/p')
  [[ -n "$object" ]] || return 0

  info ""
  info "  $object is a shared library, not a python module."
  local dirs
  dirs=$("$PY" -c '
import site, sys
seen = []
try:
    seen += list(site.getsitepackages())
except AttributeError:
    pass
seen += [p for p in sys.path if p.endswith("site-packages") or p.endswith("dist-packages")]
for d in dict.fromkeys(seen):
    print(d)
' 2>/dev/null)

  local exact="" similar="" needer=""
  while IFS= read -r dir; do
    [[ -n "$dir" && -d "$dir" ]] || continue
    while IFS= read -r hit; do
      [[ -n "$hit" ]] || continue
      # The loader asked for this name exactly. A file whose name merely
      # starts with it -- libonnxruntime.so.1.23.2, shipped by a different
      # package -- is not what is missing, and offering it as the fix sends
      # the reader to the wrong file.
      if [[ "$(basename "$hit")" == "$object" ]]; then
        exact="$exact$hit"$'\n'
      else
        similar="$similar$hit"$'\n'
      fi
    done < <(find "$dir" -name "$object*" 2>/dev/null)
    while IFS= read -r hit; do
      [[ -n "$hit" ]] && needer="$needer$hit"$'\n'
    done < <(find "$dir/$module" -name "_${module}*.so" 2>/dev/null)
  done <<< "$dirs"

  if [[ -n "$needer" ]]; then
    info "  the module that needs it:"
    echo "$needer" | sed '/^$/d;s/^/          /'
  fi

  if [[ -z "$exact" ]]; then
    info "  no file of that exact name exists under the interpreter's site"
    info "  directories, so the wheel carrying it did not install. sherpa-onnx"
    info "  splits its native libraries into a separate sherpa-onnx-core"
    info "  distribution; install that for this interpreter."
    if [[ -n "$similar" ]]; then
      info "  (these have similar names but are not it:)"
      echo "$similar" | sed '/^$/d;s/^/          /'
    fi
    return 0
  fi

  info "  found at:"
  echo "$exact" | sed '/^$/d;s/^/          /'
  [[ -n "$needer" ]] || return 0

  local lib_dir module_dir
  lib_dir=$(dirname "$(echo "$exact" | sed '/^$/d' | head -1)")
  module_dir=$(dirname "$(echo "$needer" | sed '/^$/d' | head -1)")
  [[ "$lib_dir" != "$module_dir" ]] || return 0

  info ""
  warn "  they are in different directories. The wheel finds its library"
  warn "  through RPATH \$ORIGIN, which looks only beside the module, so"
  warn "  this split is the failure:"
  info "    library  $lib_dir"
  info "    module   $module_dir"
  info ""
  info "  Confirm without changing anything:"
  info "    LD_LIBRARY_PATH=$lib_dir $PY -c 'import $module'"

  # The split is per distribution, not per file: the half that landed in the
  # other directory carries every native library, not only the one the loader
  # happened to ask for first. Copying one at a time would just move the error
  # along to the next name.
  local pending=()
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    [[ -e "$module_dir/$(basename "$candidate")" ]] || pending+=("$candidate")
  done < <(find "$lib_dir" -maxdepth 1 -name '*.so*' 2>/dev/null | sort)

  if [[ ${#pending[@]} -eq 0 ]]; then
    return 0
  fi

  info ""
  info "  ${#pending[@]} file(s) are in $lib_dir but not beside the module:"
  local file
  for file in "${pending[@]}"; do
    info "    $(basename "$file")  ($(du -h "$file" 2>/dev/null | cut -f1))"
  done

  if [[ $FIX_SPLIT -eq 0 ]]; then
    info ""
    info "  Re-run with --fix-split to copy them into"
    info "    $module_dir"
    info "  Nothing else is touched, and the originals stay where they are."
    return 0
  fi

  info ""
  info "  copying into $module_dir"
  local copier=()
  [[ -w "$module_dir" ]] || copier=(sudo)
  for file in "${pending[@]}"; do
    if "${copier[@]}" cp -p "$file" "$module_dir/"; then
      ok "    $(basename "$file")"
    else
      bad "    $(basename "$file") could not be copied"
      return 0
    fi
  done

  if "$PY" -c "import $module" >/dev/null 2>&1; then
    ok "  $module imports now"
    FIXED_SPLIT=1
  else
    bad "  $module still does not import; the error above will have changed"
  fi
}

step() { printf '\n\033[1m----- %s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mSTOP\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$*"; }

LOG="/tmp/tts_runtime_deps_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

# ------------------------------------------------------- 1. the interpreter
step "1. Which interpreter does the runtime use?"

LIB=""
SOURCE=""
if [[ -n "${TEN_PYTHON_LIB_PATH:-}" ]]; then
  LIB="$TEN_PYTHON_LIB_PATH"
  SOURCE="TEN_PYTHON_LIB_PATH"
elif [[ -r "$TASK_LOG" ]]; then
  # The runtime says so on every start; take it from the last run rather than
  # assume, because the variable is only set in the shell that launched it.
  LIB=$(grep -o 'libpython3\.[0-9]*\.so[^ ]*' "$TASK_LOG" 2>/dev/null | tail -1)
  [[ -n "$LIB" ]] && SOURCE="$TASK_LOG"
fi

if [[ -z "$LIB" ]]; then
  bad "cannot tell which interpreter the runtime uses."
  info "Either export TEN_PYTHON_LIB_PATH, or run the app once so that"
  info "$TASK_LOG records the libpython it loaded, then run this again."
  exit 1
fi
info "from $SOURCE: $LIB"

VERSION=$(basename "$LIB" | grep -oE '3\.[0-9]+')
if [[ -z "$VERSION" ]]; then
  bad "no version could be read out of $(basename "$LIB")"
  exit 1
fi
PY="python$VERSION"
if ! command -v "$PY" >/dev/null 2>&1; then
  bad "$PY is not on PATH, so its packages cannot be managed from here"
  exit 1
fi
ok "runtime interpreter: $($PY -c 'import sys; print(sys.executable, sys.version.split()[0])')"

SHELL_PY=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
if [[ "$SHELL_PY" != "$VERSION" ]]; then
  warn "the shell's python3 is $SHELL_PY -- packages installed with a bare"
  warn "pip go there, where the runtime will never look. That is the trap"
  warn "this script exists to close."
fi

# ---------------------------------------------------------- 2. what is there
step "2. What does that interpreter already have?"

MISSING=()
BROKEN=0
for module in "${PACKAGES[@]}"; do
  if line=$("$PY" -c "
import $module
print('%s %s' % (getattr($module, '__version__', '?'), $module.__file__))
" 2>&1); then
    ok "$module $line"
  elif echo "$line" | grep -q "No module named"; then
    info "$module: not installed"
    MISSING+=("${DIST[$module]}")
  else
    # Installed, and still not importable. Reinstalling will not help, and
    # adding it to the install list would hide the real fault behind a
    # successful-looking install.
    bad "$module is installed but does not import"
    echo "$line" | sed 's/^/        /'
    FIXED_SPLIT=0
    diagnose_missing_object "$module" "$line"
    if [[ $FIXED_SPLIT -eq 1 ]]; then
      # Repaired in place, so this module is no longer a reason to stop.
      ok "$module recovered"
    else
      BROKEN=1
    fi
  fi
done

if [[ $BROKEN -eq 1 ]]; then
  step "Stopping"
  bad "a dependency is present but unusable; installing more will not fix it"
  info "The diagnosis above names the file and where it is."
  info "log: $LOG"
  exit 1
fi

if [[ ${#MISSING[@]} -eq 0 ]]; then
  step "Nothing to do"
  ok "the runtime interpreter can import all of: ${PACKAGES[*]}"
  info "log: $LOG"
  exit 0
fi
info "to install: ${MISSING[*]}"

# ------------------------------------------------------------ 3. the tool
step "3. Which installer can target it?"

INSTALLER=()
if "$PY" -m pip --version >/dev/null 2>&1; then
  INSTALLER=("$PY" -m pip install)
  ok "$PY -m pip"
elif UV=$(command -v uv 2>/dev/null); then
  # --python pins the target; --system keeps uv from wanting a virtualenv.
  INSTALLER=("$UV" pip install --system --python "$(command -v "$PY")")
  ok "uv at $UV, targeting $PY"
else
  bad "$PY has no pip and uv is not on PATH"
  info "Install one of them, then run this again."
  exit 1
fi

# The target directory is root-owned on this board, so the install needs sudo.
TARGET=$("$PY" -c 'import sysconfig; print(sysconfig.get_path("purelib"))' 2>/dev/null)
info "target directory: ${TARGET:-unknown}"
if [[ -n "$TARGET" && ! -w "$TARGET" ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO=(sudo)
    warn "$TARGET is not writable; the install will use sudo"
  else
    bad "$TARGET is not writable and sudo is not available"
    exit 1
  fi
fi

# ------------------------------------------------------------- 4. the plan
step "4. What would change"
PLAN=("${SUDO[@]}" "${INSTALLER[@]}" --dry-run "${MISSING[@]}")
info "${PLAN[*]}"
"${PLAN[@]}"
rc=$?
if [[ $rc -ne 0 ]]; then
  warn "this installer cannot rehearse the change (exit $rc)"
  if [[ $CHECK_ONLY -eq 0 && $ASSUME_YES -eq 0 ]]; then
    # The board runs a vendor-patched distribution and has no backup, so an
    # install nobody has seen the plan for is not something to do silently.
    bad "refusing to install without knowing what it would change"
    info "Either use an installer that supports --dry-run (uv does), or"
    info "re-run with --yes to accept an unrehearsed install of:"
    info "  ${MISSING[*]}"
    info "log: $LOG"
    exit 1
  fi
fi

if [[ $CHECK_ONLY -eq 1 ]]; then
  step "Stopping here (--check)"
  info "log: $LOG"
  exit 0
fi

# ---------------------------------------------------------- 5. the install
step "5. Installing"
CMD=("${SUDO[@]}" "${INSTALLER[@]}" "${MISSING[@]}")
info "${CMD[*]}"
if ! "${CMD[@]}"; then
  bad "the install failed; nothing below is meaningful"
  exit 1
fi

# --------------------------------------------------------------- 6. verify
step "6. Verifying with the runtime's own interpreter"
FAILED=0
for module in "${PACKAGES[@]}"; do
  if line=$("$PY" -c "
import $module
print('%s %s' % (getattr($module, '__version__', '?'), $module.__file__))
" 2>&1); then
    ok "$module $line"
  else
    bad "$module still not importable by $PY"
    echo "$line" | sed 's/^/        /'
    FAILED=1
    diagnose_missing_object "$module" "$line"
  fi
done

step "Result"
if [[ $FAILED -eq 0 ]]; then
  ok "the runtime can now import every dependency"
  info "Next: restart the app, since Python modules are read at start."
  info "  cd $REPO_ROOT/ai_agents/agents/examples/voice-assistant"
  info "  task run 2>&1 | tee /tmp/task_run.log"
else
  bad "something is still missing; the graph will fail to load the tts node"
  info "The import errors above name it."
fi
info "log: $LOG"
exit $FAILED
