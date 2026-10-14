#!/usr/bin/env bash
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
# Install Qwen3.5-4B on cooper-vllm (Ambarella's vLLM 0.9.2) on the N1-655,
# after the vendor's deployment guide of 2026-10-12, so that its meeting
# summaries can be compared with the DeepSeek 7B the meeting graph uses.
#
#   tools/ambarella/install_qwen35_vllm.sh            check, install, test
#   tools/ambarella/install_qwen35_vllm.sh --check    check only; nothing changes
#   tools/ambarella/install_qwen35_vllm.sh --no-test  install, without the test
#
# Run it on the board as the desktop user (lychee), not with sudo. It asks
# sudo for dnf, and in the test for stopping and starting ambarella-llm.
# vLLM is built on the board, so it takes long. It can be run again at any
# point -- after a dropped SSH login, or after fixing what it reported --
# and skips what is done. Everything is also appended to
# ~/workspace/qwen35_install.log.
#
# What it cannot fetch, it needs on the board first. From Ambarella:
#   ~/demo_resources/llm_demo/qwen3.5_4B   the model package
#                                          (qwen3.5-4b_n1655-video: ranch/ and
#                                          the ViT binary); QWEN_PKG=... if elsewhere
# From the vendor's guide folder on the dev machine, into ~/workspace:
#   support-qwen3.5-VL.diff.zip            the Qwen3.5 patch for cooper-vllm
#   libamba-shepherd-n1_655.so.1.6.9       only if the board's Shepherd is
#                                          older than 1.6.9
# e.g. from the dev machine's checkout:
#   scp ambarella_ai_developer_kit_doc/vllm-qwen3.5-guide/{support-qwen3.5-VL.diff.zip,libamba-shepherd-n1_655.so.1.6.9} lychee@BOARD:workspace/
# and the internet: GitHub, PyPI, download.pytorch.org, hf-mirror.com.
#
# Steps, each skipped when done:
#   1 packages     python3.12 and its headers, gcc-c++, cmake, git, unzip,
#                  curl, cjson (dnf)
#   2 sources      cooper-vllm vLLM_0.9.2_cooper_2.5.4 and transformers_amba_ext
#                  cooper_2.5.4 (a tag), into ~/workspace
#   3 venv         python3.12, cooper-vllm/venv_vllm
#   4 base         the guide's pins; CPU torch 2.7.0 first, so that nothing
#                  pulls a CUDA one
#   5 vLLM's deps  requirements/cpu.txt; outlines and xgrammar left out of
#                  requirements/common.txt when aarch64 has no wheel for them
#   6 amba ext     transformers_amba_ext changed for Qwen3.5 (qwen35_prepare.py
#                  amba-ext), its wheel built and installed
#   7 patch        the Qwen3.5 patch applied to cooper-vllm
#   8 vLLM         built and installed (0.9.2+cpu)
#   9 FastAPI      the guide's pins, or /v1/models answers 500
#  10 Shepherd     when the system's is older than 1.6.9: the vendor's 1.6.9
#                  in ~/workspace/shepherd-1.6.9, which serve_qwen35.sh puts on
#                  LD_LIBRARY_PATH for vLLM alone. The guide relinks the
#                  system's library instead; here it and whatever else uses it
#                  stay as they are.
#  11 model        ranch/ prepared (qwen35_prepare.py model)
#  12 test         serve_qwen35.sh start, test, stop: the 7B daemon is stopped
#                  for it and given back afterwards, whatever happens

set -uo pipefail

MODE=install
TEST=1
for arg in "$@"; do
  case "$arg" in
    --check) MODE=check ;;
    --no-test) TEST=0 ;;
    -h|--help) sed -n '6,57p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK="${WORK:-$HOME/workspace}"
VENDOR_DIR="${VENDOR_DIR:-$WORK}"
QWEN_PKG="${QWEN_PKG:-$HOME/demo_resources/llm_demo/qwen3.5_4B}"
SHEPHERD_DIR="${SHEPHERD_DIR:-$WORK/shepherd-1.6.9}"
PY="${PY:-python3.12}"
VLLM_DIR="$WORK/cooper-vllm"
EXT_DIR="$WORK/transformers_amba_ext"
VENV="$VLLM_DIR/venv_vllm"
STATE="$WORK/.qwen35-install"
LOG="$WORK/qwen35_install.log"
VLLM_REF="vLLM_0.9.2_cooper_2.5.4"
EXT_REF="cooper_2.5.4"
SHEPHERD="libamba-shepherd-n1_655.so"
SHEPHERD_NEED="1.6.9"
TORCH_INDEX="https://download.pytorch.org/whl/cpu"
export WORK QWEN_PKG SHEPHERD_DIR

problems=0
ok() { echo "  ok    $*"; }
note() { echo "  note  $*"; }
problem() {
  problems=$((problems + 1))
  echo "  FAIL  $1"
  [[ -n "${2:-}" ]] && echo "        -> $2"
  return 0
}
fail() {
  echo "  FAIL  $1"
  [[ -n "${2:-}" ]] && echo "        -> $2"
  echo "  log: $LOG"
  exit 1
}
done_() { [[ -f "$STATE/$1" ]]; }
mark() { touch "$STATE/$1"; }
pip_() { "$VENV/bin/python" -m pip "$@"; }

mkdir -p "$WORK" "$STATE"
exec > >(tee -a "$LOG") 2>&1
echo "== $(date '+%F %T') install_qwen35_vllm.sh $* (as $(id -un))"

# --------------------------------------------------------------------------
# preflight

version_of() { # libamba-shepherd-n1_655.so.1.6.4 -> 1.6.4
  basename "$1" | sed -n 's/.*\.so\.\([0-9][0-9.]*\)$/\1/p'
}
at_least() { # at_least 1.6.4 1.6.9
  [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" == "$2" ]]
}

missing_pkgs=()
system_shepherd=""
need_private_shepherd=0
vendor_shepherd=""
patch_file=""

preflight() {
  echo "== preflight"
  if [[ $(id -u) -eq 0 ]]; then
    problem "running as root" "run it as the desktop user (lychee), without sudo"
  fi
  if [[ $(uname -m) == aarch64 ]]; then
    ok "aarch64"
  else
    problem "this machine is $(uname -m): run it on the board"
  fi

  command -v git >/dev/null || missing_pkgs+=(git)
  command -v curl >/dev/null || missing_pkgs+=(curl)
  command -v unzip >/dev/null || missing_pkgs+=(unzip)
  command -v g++ >/dev/null || missing_pkgs+=(gcc-c++)
  command -v cmake >/dev/null || missing_pkgs+=(cmake)
  if command -v "$PY" >/dev/null; then
    local inc
    inc=$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["include"])' 2>/dev/null)
    [[ -f "$inc/Python.h" ]] || missing_pkgs+=(python3.12-devel)
  else
    missing_pkgs+=(python3.12 python3.12-devel)
  fi
  local ldc
  ldc=$(command -v ldconfig || echo /usr/sbin/ldconfig)
  "$ldc" -p 2>/dev/null | grep -q 'libcjson\.so\.1 ' || missing_pkgs+=(cjson)
  if ((${#missing_pkgs[@]})); then
    note "to install with dnf: ${missing_pkgs[*]}"
  else
    ok "system packages are there"
  fi

  if ! done_ model; then
    local down=()
    for url in https://github.com https://pypi.org/simple/pip/ "$TORCH_INDEX/" https://hf-mirror.com; do
      [[ $(curl -s -o /dev/null -m 15 -w '%{http_code}' "$url") =~ ^[23] ]] || down+=("$url")
    done
    if ((${#down[@]})); then
      problem "not reachable: ${down[*]}" "the install downloads from all four; connect the board to the internet"
    else
      ok "GitHub, PyPI, the PyTorch index and hf-mirror.com are reachable"
    fi
  fi

  local free
  free=$(df -Pm "$WORK" | awk 'NR==2 {print $4}')
  if done_ vllm; then
    ok "$free MB free under $WORK"
  elif ((free < 6000)); then
    problem "$free MB free under $WORK; the install needs about 6 GB"
  else
    ok "$free MB free under $WORK"
  fi

  if [[ -f "$VENDOR_DIR/be6dbd5.diff" ]]; then
    patch_file="$VENDOR_DIR/be6dbd5.diff"
  elif [[ -f "$VENDOR_DIR/support-qwen3.5-VL.diff.zip" ]]; then
    patch_file="$VENDOR_DIR/support-qwen3.5-VL.diff.zip"
  fi
  if done_ patch; then
    ok "the Qwen3.5 patch is applied"
  elif [[ -n "$patch_file" ]]; then
    ok "the Qwen3.5 patch: $patch_file"
  else
    problem "no support-qwen3.5-VL.diff.zip in $VENDOR_DIR" \
      "copy it from the dev machine: scp ambarella_ai_developer_kit_doc/vllm-qwen3.5-guide/support-qwen3.5-VL.diff.zip lychee@BOARD:workspace/"
  fi

  local link
  for link in "/usr/lib64/$SHEPHERD.1" "/lib64/$SHEPHERD.1"; do
    [[ -e "$link" ]] && { system_shepherd=$(readlink -f "$link"); break; }
  done
  if [[ -z "$system_shepherd" ]]; then
    problem "no $SHEPHERD.1 on the system" \
      "transformers_amba_ext finds Shepherd through the system's copy (ldconfig); the board's BSP installs it"
  else
    local have
    have=$(version_of "$system_shepherd")
    if at_least "${have:-0}" "$SHEPHERD_NEED"; then
      ok "system Shepherd is $have ($system_shepherd): new enough"
    else
      need_private_shepherd=1
      note "system Shepherd is ${have:-unknown} ($system_shepherd); Qwen3.5 needs $SHEPHERD_NEED or newer"
      if [[ -f "$SHEPHERD_DIR/$SHEPHERD.$SHEPHERD_NEED" ]]; then
        vendor_shepherd="$SHEPHERD_DIR/$SHEPHERD.$SHEPHERD_NEED"
      elif [[ -f "$VENDOR_DIR/$SHEPHERD.$SHEPHERD_NEED" ]]; then
        vendor_shepherd="$VENDOR_DIR/$SHEPHERD.$SHEPHERD_NEED"
      fi
      if [[ -z "$vendor_shepherd" ]]; then
        problem "no $SHEPHERD.$SHEPHERD_NEED in $VENDOR_DIR" \
          "copy it from the dev machine: scp ambarella_ai_developer_kit_doc/vllm-qwen3.5-guide/$SHEPHERD.$SHEPHERD_NEED lychee@BOARD:workspace/"
      else
        local lost
        lost=$(LD_LIBRARY_PATH="$SHEPHERD_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ldd "$vendor_shepherd" 2>/dev/null |
          awk '/not found/ {print $1}' | tr '\n' ' ')
        lost=${lost% }
        if [[ -n "$lost" && "$lost" != "libcjson.so.1" ]]; then
          problem "Shepherd $SHEPHERD_NEED needs libraries this board does not have: $lost" \
            "they are not the names the board's own Shepherd uses (see below); ask Ambarella which BSP package provides them"
          ls /usr/lib64/libamba-cavalry-mem* /usr/lib64/libamba-nnctrl* /usr/lib64/libcavalry_mem* \
            /usr/lib64/libnnctrl* 2>/dev/null | sed 's/^/        board has: /'
        else
          ok "Shepherd $SHEPHERD_NEED: $vendor_shepherd, its libraries are there${lost:+ (but $lost, from step 1)}"
        fi
      fi
    fi
  fi

  if [[ -f "$QWEN_PKG/ranch/info/config.json" ]]; then
    python3 -I "$HERE/qwen35_prepare.py" model "$QWEN_PKG" --check ||
      problem "the model package at $QWEN_PKG is not as expected (above)"
  else
    problem "no Qwen3.5-4B package at $QWEN_PKG (ranch/info/config.json)" \
      "put the package from Ambarella there, or run with QWEN_PKG=/path/to/it"
    find "$HOME/demo_resources" -maxdepth 4 -iname '*qwen3*5*' 2>/dev/null | head -5 | sed 's/^/        found: /'
  fi

  if ((problems)); then
    echo
    echo "  $problems problem(s) above; nothing was changed. Fix them and run this again."
    echo "  log: $LOG"
    exit 1
  fi
}

# --------------------------------------------------------------------------
# the steps

step_packages() {
  echo "== 1 packages"
  if ((${#missing_pkgs[@]})); then
    sudo dnf install -y "${missing_pkgs[@]}" || fail "dnf install ${missing_pkgs[*]} failed"
    ok "installed ${missing_pkgs[*]}"
  else
    ok "nothing to install"
  fi
}

clone() { # url ref dir
  if [[ -d "$3/.git" ]]; then
    ok "$(basename "$3") is there ($(git -C "$3" describe --tags --always 2>/dev/null))"
    return
  fi
  git -c advice.detachedHead=false clone -q --branch "$2" --depth 1 "$1" "$3" || fail "git clone $1 ($2) failed"
  ok "cloned $(basename "$3") $2 ($(git -C "$3" rev-parse --short HEAD))"
}

step_sources() {
  echo "== 2 sources"
  clone https://github.com/Ambarella-Inc/cooper-vllm.git "$VLLM_REF" "$VLLM_DIR"
  clone https://github.com/Ambarella-Inc/transformers_amba_ext.git "$EXT_REF" "$EXT_DIR"
}

step_venv() {
  echo "== 3 venv"
  if done_ venv; then ok "$VENV"; return; fi
  [[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV" || fail "$PY -m venv failed"
  pip_ install -q -U pip wheel || fail "pip install -U pip wheel failed"
  mark venv
  ok "$VENV ($("$VENV/bin/python" --version))"
}

step_base() {
  echo "== 4 base"
  if done_ base; then ok "done before"; return; fi
  pip_ install "cmake>=3.26" ninja packaging "setuptools-scm>=8" numpy || fail "pip: build tools"
  pip_ install filelock==3.16.1 setuptools==77.0.3 transformers==4.52.4 huggingface-hub==0.36.0 ||
    fail "pip: transformers and its pins"
  pip_ install torch==2.7.0 torchaudio==2.7.0 torchvision==0.22.0 --extra-index-url "$TORCH_INDEX" ||
    fail "pip: CPU torch 2.7.0"
  mark base
  ok "transformers 4.52.4, CPU torch 2.7.0"
}

step_requirements() {
  echo "== 5 vLLM's dependencies"
  if done_ requirements; then ok "done before"; return; fi
  if ! (cd "$VLLM_DIR" && pip_ install -r requirements/cpu.txt --extra-index-url "$TORCH_INDEX"); then
    note "requirements/cpu.txt did not install; leaving out outlines and xgrammar, as the guide does on aarch64"
    local common="$VLLM_DIR/requirements/common.txt"
    [[ -f "$common.bak" ]] || cp "$common" "$common.bak"
    sed -i -E 's/^((outlines|xgrammar)[ =<>~!;].*)$/# \1  # no aarch64 wheel (install_qwen35_vllm.sh)/' "$common"
    (cd "$VLLM_DIR" && pip_ install -r requirements/cpu.txt --extra-index-url "$TORCH_INDEX") ||
      fail "requirements/cpu.txt did not install even without outlines and xgrammar"
  fi
  mark requirements
  ok "requirements/cpu.txt"
}

step_amba_ext() {
  echo "== 6 transformers_amba_ext"
  python3 -I "$HERE/qwen35_prepare.py" amba-ext "$EXT_DIR" || fail "could not change transformers_amba_ext for Qwen3.5"
  local stamp
  stamp=$(md5sum "$EXT_DIR/src/transformers_amba_ext/models/auto/modeling_auto.py" | cut -c1-12)
  if done_ "ext-$stamp" && pip_ show transformers_amba_ext >/dev/null 2>&1; then
    ok "wheel built and installed from this source before"
    return
  fi
  (cd "$EXT_DIR" && "$VENV/bin/python" setup.py bdist_wheel) || fail "transformers_amba_ext: setup.py bdist_wheel failed"
  local wheel
  wheel=$(ls -t "$EXT_DIR"/dist/transformers_amba_ext-*.whl 2>/dev/null | head -1)
  [[ -n "$wheel" ]] || fail "no wheel in $EXT_DIR/dist"
  pip_ install --force-reinstall --no-deps "$wheel" || fail "pip install $wheel failed"
  mark "ext-$stamp"
  ok "installed $(basename "$wheel")"
}

step_patch() {
  echo "== 7 the Qwen3.5 patch"
  if done_ patch; then ok "applied before"; return; fi
  local diff="$patch_file"
  if [[ "$diff" == *.zip ]]; then
    rm -rf "$STATE/patch"
    unzip -q -o "$diff" -d "$STATE/patch" || fail "unzip $diff failed"
    diff=$(ls "$STATE"/patch/*.diff 2>/dev/null | head -1)
    [[ -n "$diff" ]] || fail "no .diff in $patch_file"
  fi
  if git -C "$VLLM_DIR" apply --reverse --check "$diff" 2>/dev/null; then
    ok "it is in cooper-vllm already"
  else
    if ! git -C "$VLLM_DIR" apply --check "$diff" 2>/dev/null; then
      # As the guide does: a changed file from an earlier try is put back.
      git -C "$VLLM_DIR" checkout -- vllm/model_executor/models/transformers_amba_ext_vlm.py
    fi
    git -C "$VLLM_DIR" apply "$diff" || fail "the patch does not apply to cooper-vllm $VLLM_REF"
    ok "applied $(basename "$diff")"
  fi
  mark patch
}

step_vllm() {
  echo "== 8 vLLM (built here; this is the long step)"
  if done_ vllm && "$VENV/bin/vllm" --version 2>/dev/null | grep -q '0\.9\.2'; then
    ok "vLLM $("$VENV/bin/vllm" --version 2>/dev/null | tail -1)"
    return
  fi
  (cd "$VLLM_DIR" && SETUPTOOLS_SCM_PRETEND_VERSION=0.9.2 VLLM_TARGET_DEVICE=cpu MAX_JOBS=2 \
    "$VENV/bin/python" setup.py install) || fail "vLLM did not build"
  local v
  v=$("$VENV/bin/vllm" --version 2>/dev/null | tail -1)
  [[ "$v" == *0.9.2* ]] || fail "vllm --version says '${v:-nothing}', not 0.9.2"
  mark vllm
  ok "vLLM $v"
}

step_fastapi() {
  echo "== 9 FastAPI pins"
  if done_ fastapi; then ok "done before"; return; fi
  pip_ install "prometheus-fastapi-instrumentator==7.1.0" "starlette==0.41.3" \
    "fastapi==0.115.12" "mistral-common[opencv]==1.5.4" || fail "pip: FastAPI pins"
  mark fastapi
  ok "prometheus-fastapi-instrumentator 7.1.0, starlette 0.41.3, fastapi 0.115.12, mistral-common 1.5.4"
}

step_shepherd() {
  echo "== 10 Shepherd"
  if ((!need_private_shepherd)); then
    ok "the system's $(version_of "$system_shepherd") will do"
    return
  fi
  mkdir -p "$SHEPHERD_DIR"
  local name="$SHEPHERD.$SHEPHERD_NEED"
  if [[ "$vendor_shepherd" != "$SHEPHERD_DIR/$name" ]]; then
    install -m 755 "$vendor_shepherd" "$SHEPHERD_DIR/$name" || fail "could not copy $vendor_shepherd"
  fi
  ln -sfn "$name" "$SHEPHERD_DIR/$SHEPHERD.1"
  # Its own name for itself (SONAME); the vendor's build calls itself this.
  ln -sfn "$name" "$SHEPHERD_DIR/libshepherd.so.1"
  local lost
  lost=$(LD_LIBRARY_PATH="$SHEPHERD_DIR" ldd "$SHEPHERD_DIR/$name" | awk '/not found/ {print $1}' | tr '\n' ' ')
  [[ -z "${lost// /}" ]] || fail "Shepherd $SHEPHERD_NEED still lacks: $lost"
  ok "$SHEPHERD_DIR/$SHEPHERD.1 -> $name, for vLLM only; the system's stays $(version_of "$system_shepherd")"
}

step_model() {
  echo "== 11 model"
  python3 -I "$HERE/qwen35_prepare.py" model "$QWEN_PKG" || fail "the model package could not be prepared"
  mark model
}

step_test() {
  echo "== 12 test"
  if ((!TEST)); then
    note "skipped (--no-test); later: serve_qwen35.sh start && serve_qwen35.sh test; serve_qwen35.sh stop"
    return
  fi
  # Whatever happens from here, the 7B daemon is given back.
  trap '"$HERE/serve_qwen35.sh" stop' EXIT
  "$HERE/serve_qwen35.sh" start || fail "Qwen3.5-4B did not start (serve_qwen35.sh start, above)"
  "$HERE/serve_qwen35.sh" test || fail "Qwen3.5-4B answered, but not as it should (above)"
  ok "Qwen3.5-4B is installed and answers"
}

preflight
if [[ $MODE == check ]]; then
  echo
  echo "  ready to install: run it again without --check"
  exit 0
fi
step_packages
step_sources
step_venv
step_base
step_requirements
step_amba_ext
step_patch
step_vllm
step_fastapi
step_shepherd
step_model
step_test
echo
echo "  done. To switch the board's LLM:"
echo "    tools/ambarella/serve_qwen35.sh start | test | stop | status"
echo "  log: $LOG"
