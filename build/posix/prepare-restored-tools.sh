#!/usr/bin/env bash
# Prepare native host tools only; the normal builder regenerates GN and links Chromium.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${1:?usage: prepare-restored-tools.sh WORKDIR linux|macos x64|arm64}"
PLATFORM="${2:?platform is required}"
ARCH="${3:?architecture is required}"
case "$PLATFORM:$ARCH" in linux:x64|linux:arm64|macos:x64|macos:arm64) ;; *) exit 2 ;; esac
HOST="$(uname -m)"
case "$HOST" in x86_64) HOST=x64 ;; aarch64) HOST=arm64 ;; esac
SYSTEM="$(uname -s)"
if [ "$HOST" != "$ARCH" ] || { [ "$PLATFORM" = linux ] && [ "$SYSTEM" != Linux ]; } || \
   { [ "$PLATFORM" = macos ] && [ "$SYSTEM" != Darwin ]; }; then
  echo "a native $PLATFORM $ARCH runner is required" >&2
  exit 2
fi
WORK="$(cd "$WORK" && pwd)"
SRC="$WORK/src"
INSPECT="$(python3 "$REPO/tools/prepare_restored_build.py" --phase inspect \
  --platform "$PLATFORM" --arch "$ARCH" --workdir "$WORK")"
field() {
  python3 -c 'import json, sys; value=json.loads(sys.argv[1]);
for key in sys.argv[2].split("."): value=value[key]
print("1" if value else "0")' "$INSPECT" "$1"
}
COMPILERS_NATIVE="$(field compilers_native)"
BINDGEN_NATIVE="$(field tools.bindgen.native)"
NODE_NATIVE="$(field tools.node.native)"
RETRIEVE=0
if [ "$PLATFORM" = macos ] && { [ "$COMPILERS_NATIVE" != 1 ] || [ "$NODE_NATIVE" != 1 ]; }; then
  RETRIEVE=1
fi
if [ "$COMPILERS_NATIVE" != 1 ] || [ "$BINDGEN_NATIVE" != 1 ] || [ "$RETRIEVE" = 1 ]; then
  if [ "${GITHUB_ACTIONS:-}" != true ]; then
    echo "restored tool downloads/builds are restricted to GitHub Actions" >&2
    exit 1
  fi
fi
# Only fixed tooling/host-link paths and known downloader endpoints are changed.
python3 - "$REPO" "$WORK" "$PLATFORM" "$ARCH" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from tools.prepare_restored_build import prepare_tooling_links, verify_tooling
verify_tooling(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[1]))
prepare_tooling_links(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
PY
cd "$SRC"
if [ "$COMPILERS_NATIVE" != 1 ] || [ "$BINDGEN_NATIVE" != 1 ] || [ "$RETRIEVE" = 1 ]; then
  python3 - "$REPO" "$SRC" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from tools.prepare_restored_build import restore_tool_endpoints
restore_tool_endpoints(Path(sys.argv[2]))
PY
fi
if [ "$PLATFORM" = linux ] && [ "$ARCH" = arm64 ] && { [ "$COMPILERS_NATIVE" != 1 ] || [ "$BINDGEN_NATIVE" != 1 ]; }; then
  python3 - "$REPO" "$SRC" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from tools.prepare_restored_build import repair_linux_arm64_tool_script
repair_linux_arm64_tool_script(Path(sys.argv[2]))
PY
fi
if [ "$PLATFORM" = macos ]; then
  if [ "$RETRIEVE" = 1 ]; then
    RESOURCE_ARCH="$ARCH"
    [ "$ARCH" != x64 ] || RESOURCE_ARCH=x86_64
    python3 "$WORK/tooling/ungoogled-chromium-macos/retrieve_and_unpack_resource.py" -p "$RESOURCE_ARCH"
    BINDGEN_NATIVE=0
  fi
  if [ "$BINDGEN_NATIVE" != 1 ]; then
    python3 tools/rust/build_bindgen.py --skip-test
  fi
else
  if [ "$ARCH" = x64 ]; then SYSROOT_ARCH=amd64; else SYSROOT_ARCH=arm64; fi
  if [ "$COMPILERS_NATIVE" != 1 ]; then
    if [ "$ARCH" = x64 ]; then
      # Matching revisions do not prove that the restored binaries can execute.
      python3 - "$SRC" <<'PY'
from pathlib import Path
import sys
src = Path(sys.argv[1])
paths = [src / name for name in (
    "third_party/rust-toolchain/VERSION",
    "third_party/llvm-build/Release+Asserts/cr_build_revision",
    "third_party/llvm-build/cr_build_revision",
    "third_party/llvm-build/force_head_revision",
)]
for path in paths:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise SystemExit(f"refusing linked toolchain stamp: {path}")
    if path.exists() and not path.is_file():
        raise SystemExit(f"invalid toolchain stamp: {path}")
for path in paths:
    path.unlink(missing_ok=True)
PY
      python3 tools/rust/update_rust.py
      python3 tools/clang/scripts/update.py
      INSPECT="$(python3 "$REPO/tools/prepare_restored_build.py" --phase inspect \
        --platform "$PLATFORM" --arch "$ARCH" --workdir "$WORK")"
      COMPILERS_NATIVE="$(field compilers_native)"
      BINDGEN_NATIVE="$(field tools.bindgen.native)"
      if [ "$COMPILERS_NATIVE" != 1 ]; then
        echo "updated Linux x64 compiler packages cannot execute on this runner" >&2
        exit 1
      fi
    else
      python3 tools/clang/scripts/build.py \
        --without-fuchsia --without-android --disable-asserts \
        --host-cc=clang --host-cxx=clang++ --use-system-cmake --with-ml-inliner-model=
      export CARGO_HOME="$SRC/third_party/rust-src/cargo-home"
      python3 tools/rust/build_rust.py --skip-test
      BINDGEN_NATIVE=0
    fi
  fi
  if [ "$ARCH" = x64 ] && [ "$BINDGEN_NATIVE" != 1 ]; then
    echo "Linux x64 Rust package lacks executable bindgen; refusing to build without native Rust LLVM intermediates" >&2
    exit 1
  fi
  if [ "${GITHUB_ACTIONS:-}" = true ]; then
    python3 build/linux/sysroot_scripts/install-sysroot.py --arch="$SYSROOT_ARCH"
  fi
  if [ "$BINDGEN_NATIVE" != 1 ]; then
    python3 tools/rust/build_bindgen.py --skip-test
  fi
fi
python3 "$REPO/tools/prepare_restored_build.py" --phase finish \
  --platform "$PLATFORM" --arch "$ARCH" --workdir "$WORK"
touch "$SRC/.chromix-toolchain-ready"
