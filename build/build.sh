#!/usr/bin/env bash
# Native Linux build using pinned ungoogled-chromium source layers.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${1:-$REPO/.chromix-build-linux}"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in x86_64) HOST_ARCH=x64 ;; aarch64) HOST_ARCH=arm64 ;; esac
ARCH="${2:-$HOST_ARCH}"
case "$ARCH" in
  x64) SYSROOT_ARCH=amd64; GO_ARCH=amd64 ;;
  arm64) SYSROOT_ARCH=arm64; GO_ARCH=arm64 ;;
  *) echo "unsupported Linux architecture: $ARCH" >&2; exit 2 ;;
esac
if [ "$(uname -s)" != Linux ] || [ "$HOST_ARCH" != "$ARCH" ]; then
  echo "a native Linux $ARCH host is required" >&2; exit 2
fi
mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
SRC="$WORK/src"
OUT="$SRC/out/Chromix"
export TMPDIR="${TMPDIR:-$WORK/tmp}"
mkdir -p "$TMPDIR"
TMPDIR="$(cd "$TMPDIR" && pwd)"
"$REPO/build/prepare-ungoogled.sh" "$WORK" linux "$ARCH"
if [ "${CHROMIX_SKIP_DEPS:-0}" != 1 ]; then
  "$SRC/build/install-build-deps.sh" --no-prompt
fi
cd "$SRC"
for tool in node go gperf clang-format ninja; do
  command -v "$tool" >/dev/null || { echo "required build tool is missing: $tool" >&2; exit 1; }
done
if ! node --input-type=module -e 'process.exit(typeof import.meta.main === "boolean" ? 0 : 1)'; then
  echo "Node.js 22.18+ or 24.2+ is required for DevTools generation" >&2
  exit 1
fi
# Refresh host-tool links when PATH changes between builds.
for node_arch in x64 "$ARCH"; do
  mkdir -p "third_party/node/linux/node-linux-$node_arch/bin"
  ln -sfn "$(command -v node)" "third_party/node/linux/node-linux-$node_arch/bin/node"
done
mkdir -p third_party/gperf/cipd/bin "third_party/dawn/tools/golang/linux-$GO_ARCH/bin" buildtools/linux64-format
ln -sfn "$(command -v gperf)" third_party/gperf/cipd/bin/gperf
ln -sfn "$(command -v go)" "third_party/dawn/tools/golang/linux-$GO_ARCH/bin/go"
ln -sfn "$(command -v clang-format)" buildtools/linux64-format/clang-format
if [ ! -f "$SRC/.chromix-toolchain-ready" ]; then
  if [ -f "$SRC/.chromix-domain-substituted" ]; then
    echo "toolchain is incomplete in a domain-substituted source tree; use a clean work directory" >&2; exit 1
  fi
  if [ "$ARCH" = x64 ]; then
    python3 tools/rust/update_rust.py
    python3 tools/clang/scripts/update.py
  else
    python3 tools/clang/scripts/build.py \
      --without-fuchsia --without-android --disable-asserts \
      --host-cc=clang --host-cxx=clang++ --use-system-cmake --with-ml-inliner-model=
    export CARGO_HOME="$SRC/third_party/rust-src/cargo-home"
    python3 tools/rust/build_rust.py --skip-test
  fi
  python3 build/linux/sysroot_scripts/install-sysroot.py --arch="$SYSROOT_ARCH"
  if [ "$ARCH" = arm64 ]; then
    python3 tools/rust/build_bindgen.py --skip-test
  fi
  test -x third_party/rust-toolchain/bin/bindgen
  touch "$SRC/.chromix-toolchain-ready"
fi
export CC="$SRC/third_party/llvm-build/Release+Asserts/bin/clang"
export CXX="$SRC/third_party/llvm-build/Release+Asserts/bin/clang++"
export AR="$SRC/third_party/llvm-build/Release+Asserts/bin/llvm-ar"
export NM="$SRC/third_party/llvm-build/Release+Asserts/bin/llvm-nm"
if [ -f "$SRC/.chromix-domain-substitution-in-progress" ]; then
  echo "domain substitution was interrupted; use a clean work directory" >&2; exit 1
fi
if [ "${CHROMIX_APPLY_DOMAIN_SUBSTITUTION:-1}" = 1 ] && [ ! -f "$SRC/.chromix-domain-substituted" ]; then
  touch "$SRC/.chromix-domain-substitution-in-progress"
  python3 "$WORK/tooling/ungoogled-chromium/utils/domain_substitution.py" apply \
    -r "$WORK/tooling/ungoogled-chromium/domain_regex.list" \
    -f "$WORK/tooling/ungoogled-chromium/domain_substitution.list" "$SRC"
  mv "$SRC/.chromix-domain-substitution-in-progress" "$SRC/.chromix-domain-substituted"
fi
mkdir -p "$OUT"
printf 'target_cpu = "%s"\nv8_target_cpu = "%s"\n' "$ARCH" "$ARCH" > "$WORK/target.gn"
python3 "$REPO/tools/merge_gn_args.py" "$OUT/args.gn" \
  "$WORK/tooling/ungoogled-chromium/flags.gn" \
  "$WORK/tooling/ungoogled-chromium-portablelinux/flags.linux.gn" \
  "$REPO/build/args.gn" "$WORK/target.gn"
if [ ! -x "$OUT/gn" ]; then
  python3 tools/gn/bootstrap/bootstrap.py -o "$OUT/gn" --skip-generate-buildfiles
fi
"$OUT/gn" gen "$OUT" --fail-on-unused-args
ninja -C "$OUT" -j "${CHROMIX_JOBS:-$(getconf _NPROCESSORS_ONLN)}" chrome chrome_crashpad_handler chrome_sandbox
"$OUT/chrome" --version
