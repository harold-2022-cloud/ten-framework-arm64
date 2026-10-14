#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Find on the board what install_qwen35_vllm.sh needs, and print it to be
# pasted back. Read-only: nothing is installed, changed or started; no sudo.
#
#   tools/ambarella/probe_qwen35_board.sh          the usual places (about a minute)
#   tools/ambarella/probe_qwen35_board.sh --deep   the whole disk, for the model
#
# Everything printed is also written to /tmp/probe_qwen35_board-<uid>.txt.
#
#   os         the system, the user
#   model      the Qwen3.5-4B package: ranch/ folders with info/config.json and
#              what they say (model_type, architectures), the ViT binary, and
#              whatever is named like Qwen3.5 -- folders, tar or zip archives
#   llm now    the LLM the board runs now: its daemon, service and model folder
#   shepherd   the libamba-shepherd versions, the one the system uses, and the
#              cavalry / nnctrl / cjson libraries the vendor's 1.6.9 needs
#   vendor     the patch and Shepherd 1.6.9, if they are on the board already
#   python     python3.12 and its headers, python3
#   build      gcc, g++, cmake, git, unzip, curl
#   space      disk under ~ and /tmp, memory
#   net        GitHub, PyPI, the PyTorch index, hf-mirror.com
#   existing   ~/workspace: checkouts, virtual environments, vllm in them
#   ports      what listens on 8000, 8080, 8081, 8765

set -uo pipefail

DEEP=0
case "${1:-}" in
  "") ;;
  --deep) DEEP=1 ;;
  -h|--help) sed -n '6,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac

OUT="/tmp/probe_qwen35_board-$(id -u).txt"
exec > >(tee "$OUT") 2>&1

say() { echo; echo "== $*"; }
kv() { printf '  %-24s %s\n' "$1" "$2"; }
first_line() { "$@" 2>&1 | head -1; }
has() { command -v "$1" >/dev/null 2>&1; }

say "os"
kv "date" "$(date '+%F %T %Z')"
kv "user" "$(id -un) (uid $(id -u))$([[ $(id -u) -eq 0 ]] && echo ' -- run it as the desktop user, not root')"
kv "system" "$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-?}") / $(uname -r) / $(uname -m)"
kv "AMBARELLA_ARCH" "${AMBARELLA_ARCH:-(not set)}"
kv "board model" "$({ tr -d '\0' < /proc/device-tree/model; } 2>/dev/null || echo '?')"

say "model (searching$([[ $DEEP -eq 1 ]] && echo ' the whole disk' || echo ' the usual places'))"
roots=()
if ((DEEP)); then
  roots=(/)
else
  for r in "$HOME" /home /opt /data /mnt /media /srv /userdata /sdcard /usr/share/ambarella /usr/local; do
    [[ -d "$r" ]] || continue
    # ~ is under /home already when /home is searched
    [[ "$r" == "$HOME" && -d /home && "$HOME" == /home/* ]] && continue
    roots+=("$r")
  done
fi
hits=$(mktemp)
# One pass; the first test that matches names what a file is.
find "${roots[@]}" -maxdepth 9 \
  \( -path /proc -o -path /sys -o -path /dev -o -path /run -o -name .git \
     -o -name node_modules -o -name site-packages -o -name .cache -o -name __pycache__ \) -prune -o \
  \( -type d -name ranch -printf 'ranch\t%p\n' \) -o \
  \( -name '*vit*cavalry*.bin' -printf 'vit\t%s\t%p\n' \) -o \
  \( \( -name 'support-qwen3.5-VL.diff*' -o -name 'be6dbd5.diff' -o -name 'libamba-shepherd*' \) \
     -printf 'vendor\t%y\t%s\t%p\n' \) -o \
  \( \( -iname '*qwen3*5*' -o -iname '*qwen35*' \) -printf 'named\t%y\t%s\t%p\n' \) \
  2>/dev/null > "$hits"

n=0
while IFS=$'\t' read -r _ path; do
  n=$((n + 1))
  cfg="$path/info/config.json"
  if [[ -f "$cfg" ]]; then
    what=$(python3 -I -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c.get("model_type"), c.get("architectures"))' "$cfg" 2>/dev/null)
    kv "ranch/" "$path  [info/config.json: ${what:-unreadable}]"
  else
    kv "ranch/" "$path  [no info/config.json]"
  fi
  [[ -e "$path/config.json" ]] && kv "" "  config.json: $(ls -l "$path/config.json" | awk '{print $1, $NF}')"
done < <(awk -F'\t' '$1 == "ranch"' "$hits" | head -20)
((n)) || kv "ranch/" "none found"
awk -F'\t' '$1 == "vit" {printf "  %-24s %s (%d MB)\n", "ViT binary", $3, $2/1048576}' "$hits" | head -10
awk -F'\t' '$1 == "named" {printf "  %-24s %s %s (%s)\n", "named like Qwen3.5", ($2 == "d" ? "dir " : ($2 == "l" ? "link" : "file")), $4, ($2 == "f" ? int($3/1048576) " MB" : "-")}' "$hits" | head -40
if ! awk -F'\t' '$1 == "named"' "$hits" | grep -q .; then kv "named like Qwen3.5" "nothing"; fi
if [[ -d "$HOME/demo_resources" ]]; then
  echo "  ~/demo_resources/models:"
  ls -la "$HOME/demo_resources/models" 2>/dev/null | tail -n +2 | awk '{print "        " $0}' | head -30
fi

say "llm now"
if pgrep -x test_llm >/dev/null; then
  kv "daemon" "running: $(ps -o args= -C test_llm | head -1 | cut -c1-200)"
else
  kv "daemon" "test_llm not running"
fi
if has systemctl && systemctl cat ambarella-llm >/dev/null 2>&1; then
  kv "ambarella-llm" "$(systemctl is-enabled ambarella-llm 2>/dev/null) / $(systemctl is-active ambarella-llm 2>/dev/null)"
  kv "" "$(systemctl show -p ExecStart --value ambarella-llm 2>/dev/null | sed -n 's/.*argv\[\]=\([^;]*\).*/\1/p' | cut -c1-200)"
else
  kv "ambarella-llm" "no such service"
fi
if [[ -d "$HOME/demo_resources/llm_demo" ]]; then
  echo "  ~/demo_resources/llm_demo:"
  ls -la "$HOME/demo_resources/llm_demo" | tail -n +2 | awk '{print "        " $0}' | head -20
fi

say "shepherd"
for d in /usr/lib64 /lib64 /usr/lib /usr/local/lib64 /usr/local/lib; do
  ls -l "$d"/libamba-shepherd* 2>/dev/null | awk '{print "  " $1 " " $5 " " $9 " " $10 " " $11}'
done | sort -u -k3
for link in /usr/lib64/libamba-shepherd-n1_655.so.1 /lib64/libamba-shepherd-n1_655.so.1; do
  [[ -e "$link" ]] && kv "system uses" "$link -> $(readlink -f "$link")"
done
LDC=$(command -v ldconfig || echo /usr/sbin/ldconfig)
echo "  ldconfig knows:"
"$LDC" -p 2>/dev/null | grep -iE 'shepherd|cavalry|nnctrl|cjson' | sed 's/^\s*/        /' | head -20
if has rpm; then
  echo "  packages:"
  rpm -qa 2>/dev/null | grep -iE 'shepherd|cavalry|nnctrl|cjson' | sort | sed 's/^/        /'
fi
echo "  the libraries the vendor's 1.6.9 needs:"
for lib in libcjson.so.1 libcavalry_mem.so.0 libnnctrl.so.3; do
  where=$("$LDC" -p 2>/dev/null | awk -v l="$lib" '$1 == l {print $NF; exit}')
  kv "  $lib" "${where:-NOT FOUND}"
done

say "vendor files already on the board"
if awk -F'\t' '$1 == "vendor"' "$hits" | grep -q .; then
  awk -F'\t' '$1 == "vendor" {printf "  %s %s (%d KB)\n", ($2 == "l" ? "link" : "file"), $4, $3/1024}' "$hits" | head -20
else
  kv "" "none in the searched places"
fi
v169=$(awk -F'\t' '$1 == "vendor" && $4 ~ /libamba-shepherd.*\.so\.1\.6\.9$/ && $2 == "f" {print $4; exit}' "$hits")
if [[ -n "$v169" ]]; then
  echo "  ldd $v169:"
  ldd "$v169" 2>&1 | grep -E 'not found|cavalry|nnctrl|cjson' | sed 's/^\s*/        /'
fi

say "python"
kv "python3.12" "$(has python3.12 && first_line python3.12 --version || echo 'not installed')"
if has python3.12; then
  inc=$(python3.12 -c 'import sysconfig; print(sysconfig.get_paths()["include"])' 2>/dev/null)
  kv "Python.h (3.12)" "$([[ -f "$inc/Python.h" ]] && echo "$inc/Python.h" || echo "missing (python3.12-devel)")"
  kv "venv / ensurepip" "$(python3.12 -c 'import venv, ensurepip; print("ok")' 2>&1 | tail -1)"
fi
kv "python3" "$(first_line python3 --version) ($(command -v python3))"

say "build tools"
for t in gcc g++ cmake git unzip curl; do
  flag=--version
  [[ $t == unzip ]] && flag=-v
  if has "$t"; then kv "$t" "$(first_line "$t" $flag | cut -c1-80)"; else kv "$t" "not installed"; fi
done
has dnf && kv "dnf" "$(first_line dnf --version)"

say "space"
df -h "$HOME" /tmp 2>/dev/null | awk 'NR == 1 || !seen[$0]++ {print "  " $0}'
free -m | awk '{print "  " $0}'
kv "cpus" "$(nproc)"

say "net"
for url in https://github.com https://pypi.org/simple/pip/ https://download.pytorch.org/whl/cpu/ https://hf-mirror.com; do
  kv "$url" "$(curl -s -o /dev/null -m 15 -w '%{http_code} in %{time_total}s' "$url" 2>/dev/null || echo unreachable)"
done

say "existing"
if [[ -d "$HOME/workspace" ]]; then
  ls -la "$HOME/workspace" | tail -n +2 | awk '{print "  " $0}' | head -30
  for d in "$HOME"/workspace/*/; do
    [[ -d "$d.git" ]] && kv "git $(basename "$d")" "$(git -C "$d" describe --tags --always 2>/dev/null) on $(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null), $(git -C "$d" status --short 2>/dev/null | wc -l) changed files"
  done
else
  kv "~/workspace" "does not exist"
fi
while IFS= read -r act; do
  venv=$(dirname "$(dirname "$act")")
  kv "venv" "$venv ($("$venv/bin/python" --version 2>&1))"
  "$venv/bin/python" -m pip list 2>/dev/null | grep -iE '^(vllm|torch|transformers|transformers-amba-ext) ' | sed 's/^/        /'
done < <(find "$HOME" -maxdepth 5 -path '*/bin/activate' 2>/dev/null | head -10)
has vllm && kv "vllm on PATH" "$(command -v vllm)"

say "ports"
if has ss; then
  ss -ltnp 2>/dev/null | awk 'NR == 1 || /:(8000|8080|8081|8765)[^0-9]/ {print "  " $0}'
fi

rm -f "$hits"
echo
echo "  saved to $OUT -- paste it back"
