#!/usr/bin/env bash
#
# Verify an installed arm64 tenapp: module stack, ELF architecture, dynamic
# linkage, the Python ABI wiring, the locally-built Go artifacts, and the
# running services.
#
# It checks an *installed* tenapp — the packages `tman install` pulled from the
# registry plus what `task install` built locally. It does NOT check a
# from-source build of the framework core; that path is separate and is called
# out at the end.
#
# Usage:
#   ./verify_arm64_install.sh [example-name]     # default: websocket-example
#
# Exit code is the number of failed checks, so it is usable in CI.

set -uo pipefail

EXAMPLE="${1:-websocket-example}"

# Repo root: two levels above agents/, resolved from this script's location so
# the script works from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_AGENTS="$(cd "$SCRIPT_DIR/../.." && pwd)"
TENAPP="$AI_AGENTS/agents/examples/$EXAMPLE/tenapp"
API_BIN="$AI_AGENTS/server/bin/api"

PASS=0
FAIL=0
SKIP=0

hdr()  { printf '\n\033[1m%s\033[0m\n%s\n' "$1" "$(printf '%.0s-' {1..60})"; }
ok()   { printf '  \033[32m✅\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m❌\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[33m—\033[0m  %s\n' "$1"; SKIP=$((SKIP+1)); }
note() { printf '       %s\n' "$1"; }

# ---------------------------------------------------------------- host
# Provenance first, verdicts second. Distribution-specific behaviour in this
# repo keys off the RPM/Debian family and the Python the binding was built
# against, and vendor rebuilds carry their own PRETTY_NAME, so record what the
# machine actually reports rather than inferring the distribution from glibc.
hdr "0. Host"

ARCH="$(uname -m)"
kv() { printf '  %-16s %s\n' "$1" "$2"; }

kv "arch"        "$ARCH"
kv "kernel"      "$(uname -r)"

if [ -r /etc/os-release ]; then
  # Read in a subshell so the sourced file cannot clobber this script's vars.
  OS_ID="$(. /etc/os-release; echo "${ID:-}")"
  OS_LIKE="$(. /etc/os-release; echo "${ID_LIKE:-}")"
  OS_VER="$(. /etc/os-release; echo "${VERSION_ID:-}")"
  OS_NAME="$(. /etc/os-release; echo "${PRETTY_NAME:-}")"
  OS_VARIANT="$(. /etc/os-release; echo "${VARIANT:-}")"
  kv "distro ID"   "${OS_ID:-unset}"
  kv "ID_LIKE"     "${OS_LIKE:-unset}"
  kv "VERSION_ID"  "${OS_VER:-unset}"
  kv "PRETTY_NAME" "${OS_NAME:-unset}"
  [ -n "$OS_VARIANT" ] && kv "VARIANT" "$OS_VARIANT"
else
  OS_ID=""; OS_LIKE=""; OS_VER=""; OS_NAME=""
  kv "os-release" "absent"
fi

# A vendor rebuild often keeps the upstream release package, which names the
# base distribution and version more reliably than PRETTY_NAME does.
if command -v rpm >/dev/null 2>&1; then
  REL_PKG="$(rpm -q --whatprovides system-release 2>/dev/null | head -2 | tr '\n' ' ')"
  kv "release pkg" "${REL_PKG:-none}"
fi
for f in /etc/fedora-release /etc/redhat-release /etc/system-release /etc/debian_version; do
  [ -r "$f" ] && kv "$(basename "$f")" "$(head -1 "$f")"
done

GLIBC="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
kv "glibc"       "${GLIBC:-unreadable}"
kv "gcc"         "$(gcc -dumpfullversion 2>/dev/null || echo absent)"
kv "pkg manager" "$(for c in dnf5 dnf yum apt-get; do command -v $c >/dev/null && { echo $c; break; }; done)"
kv "SELinux"     "$(getenforce 2>/dev/null || echo 'n/a')"

CONTAINER="no"
if [ -f /.dockerenv ] || [ -f /run/.containerenv ]; then CONTAINER="yes"; fi
command -v systemd-detect-virt >/dev/null 2>&1 &&
  { v="$(systemd-detect-virt -c 2>/dev/null)"; [ "$v" != "none" ] && CONTAINER="$v"; }
kv "container"   "$CONTAINER"

CPU_NAME="$(lscpu 2>/dev/null | awk -F': +' '/^(Model name|BIOS Model name)/{print $2; exit}')"
[ -n "$CPU_NAME" ] || CPU_NAME="$(awk -F': ' '/^(model name|Model)/{print $2; exit}' /proc/cpuinfo 2>/dev/null)"
[ -n "$CPU_NAME" ] || CPU_NAME="$(lscpu 2>/dev/null | awk -F': +' '/^Vendor ID/{print "implementer "$2; exit}')"
kv "cpu"         "${CPU_NAME:-unknown}"
kv "cores / mem" "$(nproc 2>/dev/null) cores, $(free -h 2>/dev/null | awk '/^Mem:/{print $2}')"

# Every interpreter the runtime could plausibly bind against.
PY_FOUND=""
for v in 3.10 3.11 3.12 3.13 3.14; do
  command -v "python$v" >/dev/null 2>&1 && PY_FOUND="$PY_FOUND python$v"
done
PY_FOUND="${PY_FOUND# }"
kv "python3"     "$(python3 -V 2>&1 | awk '{print $2}') ($(command -v python3))"
kv "python 3.x"  "${PY_FOUND:-none besides python3}"
LIBPY="$(ls /usr/lib64/libpython3*.so* /usr/lib/*/libpython3*.so* 2>/dev/null | tr '\n' ' ')"
kv "libpython"   "${LIBPY:-none}"

kv "tenapp"      "$TENAPP"

printf '\n'
if [ "$ARCH" = "aarch64" ]; then ok "host is aarch64"
else bad "host is $ARCH, not aarch64"; fi

if [ -n "$GLIBC" ] && [ "$(printf '2.38\n%s\n' "$GLIBC" | sort -V | head -1)" = "2.38" ]; then
  ok "glibc $GLIBC meets the 2.38 floor for prebuilt arm64 packages"
else
  bad "glibc ${GLIBC:-unreadable} is below the 2.38 floor"
  note "prebuilt arm64 packages will not load; build the core from source"
fi

# ID_LIKE is optional, and an independent RPM distribution leaves it unset. Fall
# back to the package database actually present -- evidence, not a declaration.
FAMILY=""
case " $OS_ID $OS_LIKE " in
  *" fedora "*|*" rhel "*|*" centos "*) FAMILY="rpm";   FAMILY_SRC="os-release" ;;
  *" debian "*|*" ubuntu "*)            FAMILY="debian"; FAMILY_SRC="os-release" ;;
esac
if [ -z "$FAMILY" ]; then
  if command -v rpm >/dev/null 2>&1 && rpm -q --whatprovides system-release >/dev/null 2>&1; then
    FAMILY="rpm"; FAMILY_SRC="rpm database"
  elif command -v dpkg >/dev/null 2>&1; then
    FAMILY="debian"; FAMILY_SRC="dpkg"
  fi
fi

case "$FAMILY" in
  rpm)    ok "RPM-based (via $FAMILY_SRC) -- dnf package names apply" ;;
  debian) ok "Debian-based (via $FAMILY_SRC) -- apt package names apply" ;;
  *)      bad "cannot determine the package family"
          note "neither os-release nor a package database identified it" ;;
esac

# Third-party installers read ID/ID_LIKE directly and refuse to run when the
# distribution is not one they enumerate, regardless of the package manager.
if [ -z "${OS_LIKE:-}" ] && [ "$FAMILY" = "rpm" ] &&
   ! printf '%s' " $OS_ID " | grep -q ' fedora \| rhel \| centos '; then
  skip "ID=$OS_ID with ID_LIKE unset -- installers keyed on os-release will refuse"
  note "NodeSource's setup script fails here; use the distribution's own nodejs"
fi

if [ ! -d "$TENAPP" ]; then
  bad "tenapp not found -- run 'task install' in agents/examples/$EXAMPLE first"
  printf '\nAborting: nothing to check.\n'
  exit $FAIL
fi

# ---------------------------------------------------------------- env
hdr "1. Environment wiring"
if [ -n "${TEN_PYTHON_LIB_PATH:-}" ]; then
  if [ -e "$TEN_PYTHON_LIB_PATH" ]; then
    ok "TEN_PYTHON_LIB_PATH -> $TEN_PYTHON_LIB_PATH"
  else
    bad "TEN_PYTHON_LIB_PATH is set to a missing file: $TEN_PYTHON_LIB_PATH"
  fi
else
  bad "TEN_PYTHON_LIB_PATH is unset"
  note "python_addon_loader falls back to libpython3.10.so and fails to load"
fi

if [ -n "${UV_PYTHON:-}" ]; then
  ok "UV_PYTHON -> $UV_PYTHON"
else
  skip "UV_PYTHON unset — fine if the system python already matches the binding"
  note "on Fedora 41+ the system python is 3.13 and dependencies land in the"
  note "wrong site-packages without it"
fi

# The interpreter the runtime will actually embed, derived from the lib path.
PY_BIN=""
if [ -n "${TEN_PYTHON_LIB_PATH:-}" ]; then
  PY_VER="$(basename "$TEN_PYTHON_LIB_PATH" | grep -oE '3\.[0-9]+')"
  [ -n "$PY_VER" ] && PY_BIN="$(command -v "python$PY_VER" || true)"
fi
[ -z "$PY_BIN" ] && PY_BIN="$(command -v python3 || true)"
note "interpreter used for import checks: ${PY_BIN:-none found}"

# ---------------------------------------------------------------- stack
hdr "2. Module stack"
for pkg in ten_runtime ten_runtime_python ten_runtime_go ten_ai_base; do
  if [ -d "$TENAPP/ten_packages/system/$pkg" ]; then ok "system package: $pkg"
  else bad "system package missing: $pkg"; fi
done

EXT_COUNT=$(find "$TENAPP/ten_packages/extension" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
if [ "$EXT_COUNT" -gt 0 ]; then ok "extensions installed: $EXT_COUNT"
else bad "no extensions found under ten_packages/extension"; fi

# ---------------------------------------------------------------- ELF arch
hdr "3. Native library architecture"
mapfile -t SOS < <(find "$TENAPP/ten_packages" -name '*.so' 2>/dev/null)
if [ "${#SOS[@]}" -eq 0 ]; then
  bad "no .so files found under ten_packages"
else
  WRONG=0
  for so in "${SOS[@]}"; do
    file -b "$so" | grep -q 'ARM aarch64' || { bad "not aarch64: $so"; WRONG=$((WRONG+1)); }
  done
  [ "$WRONG" -eq 0 ] && ok "all ${#SOS[@]} shared objects are ARM aarch64"
fi

# ---------------------------------------------------------------- linkage
hdr "4. Dynamic linkage"
# `ldd` on a plugin is not the same question as "is this install broken". An
# addon that the runtime dlopens into a process already holding libten_runtime
# does not need to resolve it standalone. So an unresolved NEEDED entry is only
# a failure when the library is absent from the tree entirely; when it is
# present, report the fact and the RUNPATH and let the reader judge.
LINK_BAD=0
LINK_SOFT=0
for so in "${SOS[@]:-}"; do
  [ -n "$so" ] || continue
  missing="$(ldd "$so" 2>/dev/null | awk '/not found/ {print $1}')"
  [ -n "$missing" ] || continue

  absent=""; elsewhere=""
  for m in $missing; do
    if [ -n "$(find "$TENAPP/ten_packages" -name "$m" -print -quit 2>/dev/null)" ]; then
      elsewhere="$elsewhere $m"
    else
      absent="$absent $m"
    fi
  done

  if [ -n "$absent" ]; then
    bad "$(basename "$so") needs libraries absent from the tree:$absent"
    LINK_BAD=$((LINK_BAD+1))
  else
    RUNPATH="$(objdump -x "$so" 2>/dev/null | awk '/RUNPATH|RPATH/ {print $2; exit}')"
    skip "$(basename "$so") does not resolve standalone:$elsewhere"
    note "all of those exist under ten_packages; RUNPATH: ${RUNPATH:-<none>}"
    note "loaded by a process that already holds them, so this is expected"
    LINK_SOFT=$((LINK_SOFT+1))
  fi
done
if [ "${#SOS[@]}" -eq 0 ]; then
  skip "no shared objects to check"
elif [ "$LINK_BAD" -eq 0 ] && [ "$LINK_SOFT" -eq 0 ]; then
  ok "every shared object resolves all its dependencies standalone"
elif [ "$LINK_BAD" -eq 0 ]; then
  ok "no shared object references a library absent from the tree"
fi

# ---------------------------------------------------------------- glibc need
hdr "5. glibc symbols required"
RT="$TENAPP/ten_packages/system/ten_runtime/lib/libten_runtime.so"
if [ -f "$RT" ] && command -v objdump >/dev/null; then
  NEED="$(objdump -T "$RT" 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1 | sed 's/GLIBC_//')"
  if [ -n "$NEED" ]; then
    if [ "$(printf '%s\n%s\n' "$NEED" "$GLIBC" | sort -V | head -1)" = "$NEED" ]; then
      ok "libten_runtime.so needs glibc $NEED, host has $GLIBC"
    else
      bad "libten_runtime.so needs glibc $NEED but host has $GLIBC"
    fi
  else
    skip "could not read GLIBC symbol versions"
  fi
else
  skip "objdump or libten_runtime.so unavailable"
fi

# ---------------------------------------------------------------- python ABI
hdr "6. Python ABI"
SYS_DIR="$TENAPP/ten_packages/system"

LOADER="$(find "$TENAPP/ten_packages" -name 'libpython_addon_loader.so' 2>/dev/null | head -1)"
if [ -n "$LOADER" ]; then
  WANT="$(strings "$LOADER" 2>/dev/null | grep -oE 'libpython3\.[0-9]+\.so' | sort -u | head -1)"
  note "addon loader's built-in dlopen target: ${WANT:-unknown}"
  if [ -n "${TEN_PYTHON_LIB_PATH:-}" ] && [ -n "$WANT" ] &&
     [ "$(basename "$TEN_PYTHON_LIB_PATH")" != "$WANT" ]; then
    ok "TEN_PYTHON_LIB_PATH overrides that default (required on arm64)"
  fi
else
  skip "libpython_addon_loader.so not found"
fi

if [ -z "$PY_BIN" ]; then
  skip "no interpreter resolved; import checks not run"
else
  if "$PY_BIN" -c 'import pydantic, aiohttp, websockets' 2>/dev/null; then
    ok "third-party deps (pydantic, aiohttp, websockets) import under $PY_BIN"
  else
    bad "third-party deps do not import under $PY_BIN"
    note "they were installed into a different interpreter -- check UV_PYTHON"
  fi

  # Reproduce the path layout the runtime itself uses: every system package's
  # interface/ on PYTHONPATH and every lib/ on the loader path, discovered
  # rather than hardcoded so a changed package set cannot silently skew this.
  PYPATH=""; LDPATH=""
  for d in "$SYS_DIR"/*/interface; do [ -d "$d" ] && PYPATH="$PYPATH:$d"; done
  for d in "$SYS_DIR"/*/lib;       do [ -d "$d" ] && LDPATH="$LDPATH:$d"; done
  PYPATH="${PYPATH#:}"; LDPATH="${LDPATH#:}"

  # Probe the binding layer and the base-class layer separately, and report the
  # exception verbatim. ten_ai_base sits on top of ten_runtime, so attributing a
  # failure to the layer that actually broke matters more than a bare verdict.
  probe() {
    PYTHONPATH="$PYPATH" \
    LD_LIBRARY_PATH="$LDPATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
      "$PY_BIN" -c "
import sys
try:
    import $1
except BaseException as e:
    sys.stdout.write('FAIL|%s: %s' % (type(e).__name__, e))
else:
    sys.stdout.write('OK|%s' % getattr($1, '__file__', '<namespace>'))
" 2>/dev/null
  }

  RT_RES="$(probe ten_runtime)"
  AB_RES="$(probe ten_ai_base)"

  case "$RT_RES" in
    OK\|*)
      ok "ten_runtime imports standalone" ;;
    *)
      skip "ten_runtime does not import standalone"
      note "${RT_RES#FAIL|}"
      note "the binding is normally loaded by the runtime process, so a failure"
      note "here is not conclusive -- section 9 is the authoritative check" ;;
  esac

  case "$AB_RES" in
    OK\|*)
      ok "ten_ai_base imports" ;;
    *)
      case "$RT_RES" in
        OK\|*)
          bad "ten_ai_base does not import while ten_runtime does"
          note "${AB_RES#FAIL|}" ;;
        *)
          skip "ten_ai_base not importable, explained by ten_runtime above"
          note "${AB_RES#FAIL|}" ;;
      esac ;;
  esac

  # Structural presence is checkable regardless of whether import works.
  for want in "$SYS_DIR/ten_ai_base/manifest.json" "$SYS_DIR/ten_ai_base/interface"; do
    if [ -e "$want" ]; then ok "present: ${want#$TENAPP/}"
    else bad "missing: ${want#$TENAPP/}"; fi
  done
fi

# ---------------------------------------------------------------- built here
hdr "7. Locally built artifacts"
for bin in "$TENAPP/bin/main" "$API_BIN"; do
  if [ -f "$bin" ]; then
    if file -b "$bin" | grep -q 'ARM aarch64'; then ok "$(basename "$bin") is ARM aarch64  ($bin)"
    else bad "$(basename "$bin") is NOT aarch64: $(file -b "$bin" | cut -c1-40)"; fi
  else
    bad "not built: $bin"
  fi
done

# ---------------------------------------------------------------- services
hdr "8. Services"
PORT_API="${SERVER_PORT:-8080}"
PORT_GD="${GRAPH_DESIGNER_SERVER_PORT:-49483}"
for p in "$PORT_API" 3000 "$PORT_GD"; do
  if ss -tln 2>/dev/null | grep -q ":$p "; then ok "port $p is listening"
  else skip "port $p not listening (services may not be started)"; fi
done

# A stopped process keeps its listening socket but never accepts, so check that
# nothing in the process group is in T state.
STOPPED="$(ps -eo stat,cmd 2>/dev/null | awk '/^T/ && (/bin\/api/ || /tman designer/ || /task run/ || /node server/) {print}' | wc -l)"
if [ "$STOPPED" -gt 0 ]; then
  bad "$STOPPED service process(es) are STOPPED (T state)"
  note "backgrounding without a controlling terminal triggers SIGTTOU"
  note "run 'task run' inside tmux instead of 'nohup ... &'"
fi

if command -v curl >/dev/null && ss -tln 2>/dev/null | grep -q ":$PORT_API "; then
  if curl -sf -m 3 "localhost:$PORT_API/health" >/dev/null 2>&1; then ok "GET /health responds"
  else bad "GET /health does not respond"; fi
  if curl -sf -m 3 "localhost:$PORT_API/graphs" 2>/dev/null | grep -q '"name"'; then
    ok "GET /graphs returns graph definitions (property.json parsed)"
  else
    bad "GET /graphs returned nothing usable"
  fi
fi

# ---------------------------------------------------------------- worker logs
hdr "9. Worker logs (the arm64 Python binding is only exercised here)"
LOG_DIR="${LOG_PATH:-/tmp/ten_agent}"
if [ -d "$LOG_DIR" ] && [ -n "$(ls -A "$LOG_DIR" 2>/dev/null)" ]; then
  if grep -qi 'Failed to load system libpython' "$LOG_DIR"/* 2>/dev/null; then
    bad "worker log contains 'Failed to load system libpython'"
    note "TEN_PYTHON_LIB_PATH is not reaching the worker process"
  else
    ok "no libpython load failure in worker logs"
  fi
  if grep -qi 'ModuleNotFoundError' "$LOG_DIR"/* 2>/dev/null; then
    bad "worker log contains ModuleNotFoundError"
    note "dependencies are in a different interpreter than the embedded one"
    grep -hoi 'ModuleNotFoundError.*' "$LOG_DIR"/* 2>/dev/null | sort -u | head -3 | sed 's/^/       /'
  else
    ok "no ModuleNotFoundError in worker logs"
  fi
else
  skip "no worker logs in $LOG_DIR — start a session to exercise the Python path"
  note "until a session runs, the Python binding has NOT been verified"
fi

# ---------------------------------------------------------------- summary
hdr "Summary"
printf '  passed  : %d\n  failed  : %d\n  skipped : %d\n' "$PASS" "$FAIL" "$SKIP"
printf '\n  Not covered by this script:\n'
printf '    - building the framework core from source (tgn gen / tgn build)\n'
printf '    - C++ extension builds\n'
printf '    - core unit/integration tests, guarder suites\n'
printf '    - RTC examples: the agora_rtc extension has no aarch64 build\n\n'

if [ "$FAIL" -eq 0 ]; then
  printf '  \033[32mAll executed checks passed.\033[0m\n\n'
else
  printf '  \033[31m%d check(s) failed.\033[0m\n\n' "$FAIL"
fi
exit "$FAIL"
