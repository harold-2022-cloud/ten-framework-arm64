#!/usr/bin/env bash

# mac, linux
OS="linux"

# x64, arm64
CPU="x64"

# debug, release
BUILD_TYPE="release"

# Map `uname -s` / `uname -m` onto the names tgn and tman understand. Used when
# <os>/<cpu> are not passed explicitly, so the same script works on x86_64 and
# arm64 hosts without every caller having to hardcode an arch.
detect_os() {
  case "$(uname -s)" in
  Linux) echo "linux" ;;
  Darwin) echo "mac" ;;
  *) echo "unsupported" ;;
  esac
}

detect_cpu() {
  case "$(uname -m)" in
  x86_64 | amd64) echo "x64" ;;
  aarch64 | arm64) echo "arm64" ;;
  *) echo "unsupported" ;;
  esac
}

PIP_INSTALL_CMD=${PIP_INSTALL_CMD:-"uv pip install --system"}

install_python_requirements() {
  local app_dir=$1

  if [[ -f "requirements.txt" ]]; then
    ${PIP_INSTALL_CMD} install -r requirements.txt
  fi

  # traverse the ten_packages/extension directory to find the requirements.txt
  if [[ -d "ten_packages/extension" ]]; then
    for extension in ten_packages/extension/*; do
      if [[ -f "$extension/requirements.txt" ]]; then
        ${PIP_INSTALL_CMD} -r $extension/requirements.txt
      fi
    done
  fi

  # traverse the ten_packages/system directory to find the requirements.txt
  if [[ -d "ten_packages/system" ]]; then
    for extension in ten_packages/system/*; do
      if [[ -f "$extension/requirements.txt" ]]; then
        ${PIP_INSTALL_CMD} -r $extension/requirements.txt
      fi
    done
  fi
}

main() {
  APP_HOME=$(
    cd $(dirname $0)/..
    pwd
  )

  if [[ $1 == "-clean" ]]; then
    clean $APP_HOME
    exit 0
  fi

  # <os> and <cpu> are optional; when omitted they are detected from the host.
  # Passing them explicitly still works and still wins.
  if [[ $# -eq 2 ]]; then
    OS=$1
    CPU=$2
  elif [[ $# -eq 0 ]]; then
    OS=$(detect_os)
    CPU=$(detect_cpu)
    echo "No <os> <cpu> given; detected host platform: $OS $CPU"
  else
    echo "Usage: $0 [<os> <cpu>]"
    echo "  os:  linux | mac   (default: detected from 'uname -s')"
    echo "  cpu: x64 | arm64   (default: detected from 'uname -m')"
    exit 1
  fi

  if [[ $OS == "unsupported" || $CPU == "unsupported" ]]; then
    echo "FATAL: unsupported host platform: $(uname -s) $(uname -m)"
    exit 1
  fi

  # AVX2 is x86-only. On an x86_64 host this probe catches an emulated x86_64
  # (Rosetta, and some QEMU builds) where the intrinsics compile but trap at
  # runtime. On arm64 the intrinsics do not exist, so the probe could only ever
  # fail to compile and pass by accident via `&&` short-circuiting -- skip it.
  if [[ $CPU == "x64" ]]; then
    echo -e "#include <stdio.h>\n#include <immintrin.h>\nint main() { __m256 a = _mm256_setzero_ps(); return 0; }" > /tmp/test.c
    if gcc -mavx2 /tmp/test.c -o /tmp/test && ! /tmp/test; then
      echo "FATAL: unsupported platform."
      echo "       Please UNCHECK the 'Use Rosetta for x86_64/amd64 emulation on Apple Silicon' Docker Desktop setting if you're running on mac."

      exit 1
    fi
  fi

  if [[ ! -f $APP_HOME/manifest.json ]]; then
    echo "FATAL: manifest.json is required."
    exit 1
  fi

  # Install all dependencies specified in manifest.json.
  echo "install dependencies..."
  tman -y install

  # install python requirements
  echo "install_python_requirements..."
  install_python_requirements $APP_HOME
}

main "$@"