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

build_cxx_extensions() {
  local app_dir=$1

  if [[ ! -f $app_dir/scripts/BUILD.gn ]]; then
    echo "FATAL: the scripts/BUILD.gn is required to build cxx extensions."
    exit 1
  fi

  cp $app_dir/scripts/BUILD.gn $app_dir

  tgn gen $OS $CPU $BUILD_TYPE -- is_clang=false enable_sanitizer=false
  tgn build $OS $CPU $BUILD_TYPE

  local ret=$?

  cd $app_dir

  if [[ $ret -ne 0 ]]; then
    echo "FATAL: failed to build cxx extensions, see logs for detail."
    exit 1
  fi

  # Copy the output of ten_packages to the ten_packages/extension/xx/lib.
  local out="out/$OS/$CPU"
  for extension in $out/ten_packages/extension/*; do
    local extension_name=$(basename $extension)
    if [[ $extension_name == "*" ]]; then
      echo "No cxx extension, nothing to copy."
      break
    fi
    if [[ ! -d $extension/lib ]]; then
      echo "No output for extension $extension_name."
      continue
    fi

    mkdir -p $app_dir/ten_packages/extension/$extension_name/lib
    cp -r $extension/lib/* $app_dir/ten_packages/extension/$extension_name/lib
  done
}

install_node_dependencies() {
  local app_dir="${1:-$PWD}"  # default to current working dir (symlink aware)

  # install in app root if package.json exists
  if [[ -f "$app_dir/package.json" ]]; then
    echo "Installing deps in $app_dir"
    (cd "$app_dir" && npm install)
  fi

  # traverse ten_packages/extension
  if [[ -d "$app_dir/ten_packages/extension" ]]; then
    for d in "$app_dir/ten_packages/extension"/*; do
      [[ -d "$d" && -f "$d/package.json" ]] || continue
      echo "Installing deps in $d"
      (cd "$d" && npm install)
    done
  fi

  # traverse ten_packages/system
  if [[ -d "$app_dir/ten_packages/system" ]]; then
    for d in "$app_dir/ten_packages/system"/*; do
      [[ -d "$d" && -f "$d/package.json" ]] || continue
      echo "Installing deps in $d"
      (cd "$d" && npm install)
    done
  fi
}


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

build_go_app() {
  local app_dir=$1
  cd $app_dir

  go run ten_packages/system/ten_runtime_go/tools/build/main.go --verbose
  if [[ $? -ne 0 ]]; then
    echo "FATAL: failed to build go app, see logs for detail."
    exit 1
  fi
}

clean() {
  local app_dir=$1
  rm -rf BUILD.gn out
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
  tman --verbose install

  # build extensions and app
  echo "build_cxx_extensions..."
  build_cxx_extensions $APP_HOME
  echo "build_go_app..."
  # build_go_app $APP_HOME
  echo "install_python_requirements..."
  install_python_requirements $APP_HOME
  echo "install_node_dependencies..."
  install_node_dependencies $APP_HOME
}

main "$@"
