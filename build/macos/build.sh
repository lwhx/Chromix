#!/usr/bin/env bash
# Native macOS build using pinned ungoogled-chromium source layers.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${1:-$REPO/.chromix-build-mac}"
HOST_ARCH="$(uname -m)"
[ "$HOST_ARCH" = x86_64 ] && HOST_ARCH=x64
ARCH="${2:-$HOST_ARCH}"
case "$ARCH" in arm64|x64) ;; *) echo "unsupported macOS architecture: $ARCH" >&2; exit 2 ;; esac
if [ "$(uname -s)" != Darwin ] || [ "$HOST_ARCH" != "$ARCH" ]; then
  echo "a native macOS $ARCH host is required" >&2; exit 2
fi
mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
SRC="$WORK/src"
OUT="$SRC/out/Chromix"
"$REPO/build/prepare-ungoogled.sh" "$WORK" macos "$ARCH"
cd "$SRC"
if [ ! -f "$SRC/.chromix-toolchain-ready" ]; then
  if [ -f "$SRC/.chromix-domain-substituted" ]; then
    echo "toolchain is incomplete in a domain-substituted source tree; use a clean work directory" >&2; exit 1
  fi
  python3 tools/rust/build_bindgen.py --skip-test
  touch "$SRC/.chromix-toolchain-ready"
fi
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
  "$WORK/tooling/ungoogled-chromium-macos/flags.macos.gn" \
  "$REPO/build/args.macos.gn" "$WORK/target.gn"
if [ ! -x "$OUT/gn" ]; then
  python3 tools/gn/bootstrap/bootstrap.py -o "$OUT/gn" --skip-generate-buildfiles
fi
"$OUT/gn" gen "$OUT" --fail-on-unused-args
ninja -C "$OUT" -j "${CHROMIX_JOBS:-$(sysctl -n hw.ncpu)}" chrome
printf '==> Done: %s\n' "$OUT/Chromium.app"
