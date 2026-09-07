#!/usr/bin/env bash
# Prepare Chromium -> ungoogled core -> platform overlay -> prune -> Chromix.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REVISION_FILE="$REPO/build/ungoogled-revisions.psd1"
WORK="${1:?usage: prepare-ungoogled.sh WORKDIR linux|macos [x64|arm64]}"
PLATFORM="${2:?platform is required}"
ARCH="${3:-x64}"
case "$ARCH" in x64|arm64) ;; *) echo "unsupported architecture: $ARCH" >&2; exit 2 ;; esac
case "$PLATFORM" in
  linux) PLATFORM_NAME=ungoogled-chromium-portablelinux; PLATFORM_KEY=UngoogledLinux ;;
  macos) PLATFORM_NAME=ungoogled-chromium-macos; PLATFORM_KEY=UngoogledMacOS ;;
  *) echo "unsupported platform: $PLATFORM" >&2; exit 2 ;;
esac
mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
CORE_REPO="$WORK/tooling/ungoogled-chromium"
PLATFORM_REPO="$WORK/tooling/$PLATFORM_NAME"
PLATFORM_PATCHES="$PLATFORM_REPO/patches"
CACHE="$WORK/download_cache"
SRC="$WORK/src"
READY="$SRC/.chromix-source-ready"
revision() {
  sed -n "s/^[[:space:]]*$1 = \"\([^\"]*\)\"/\1/p" "$REVISION_FILE"
}
CHROMIUM_VERSION="$(revision ChromiumVersion)"
CORE_COMMIT="$(revision UngoogledCommit)"
PLATFORM_COMMIT="$(revision "${PLATFORM_KEY}Commit")"
PLATFORM_VERSION="$(revision "${PLATFORM_KEY}Version")"
PATCH_HASH="$(python3 - "$REPO" <<'PY'
import hashlib
import sys
from pathlib import Path
repo = Path(sys.argv[1])
hash = hashlib.sha256()
paths = [repo / 'build/prepare-ungoogled.sh', repo / 'build/apply-patches.sh', repo / 'patches/series']
for line in (repo / 'patches/series').read_text().splitlines():
    name = line.split('#', 1)[0].strip()
    if name:
        paths.append(repo / name)
paths.extend(sorted((repo / 'build/windows/lite-tarball-files').rglob('*')))
for path in paths:
    if path.is_file():
        hash.update(str(path.relative_to(repo)).encode())
        hash.update(path.read_bytes())
print(hash.hexdigest())
PY
)"
KEY="$PLATFORM|$ARCH|$CHROMIUM_VERSION|$CORE_COMMIT|$PLATFORM_COMMIT|$PATCH_HASH"
if [ -f "$READY" ] && [ "$(cat "$READY")" = "$KEY" ]; then
  echo "==> pinned ungoogled source already prepared: $KEY"
  exit 0
fi
if [ -e "$SRC" ]; then
  echo "source is incomplete or has different pins; use a clean work directory: $WORK" >&2
  exit 1
fi
mkdir -p "$WORK/tooling" "$CACHE"
checkout_pinned() {
  local url="$1" path="$2" commit="$3"
  if [ ! -d "$path/.git" ]; then
    git init "$path"
    git -C "$path" remote add origin "$url"
  fi
  git -C "$path" fetch --depth 1 origin "$commit"
  git -C "$path" checkout --detach --force "$commit"
  test "$(git -C "$path" rev-parse HEAD)" = "$commit"
}
checkout_pinned https://github.com/ungoogled-software/ungoogled-chromium.git "$CORE_REPO" "$CORE_COMMIT"
checkout_pinned "https://github.com/ungoogled-software/$PLATFORM_NAME.git" "$PLATFORM_REPO" "$PLATFORM_COMMIT"
test "$(cat "$CORE_REPO/chromium_version.txt")" = "$CHROMIUM_VERSION"
CORE_VERSION="$CHROMIUM_VERSION-$(cat "$CORE_REPO/revision.txt")"
test "$CORE_VERSION" = "$(revision UngoogledVersion)"
if [ "$PLATFORM" = macos ]; then
  test "$CORE_VERSION.$(cat "$PLATFORM_REPO/revision.txt")" = "$PLATFORM_VERSION"
else
  test "$CORE_VERSION" = "$PLATFORM_VERSION"
fi
PATCH_BIN="${PATCH_BIN:-$(command -v gpatch || command -v patch)}"
export PATCH_BIN
mkdir -p "$SRC"
python3 "$CORE_REPO/utils/downloads.py" retrieve -i "$CORE_REPO/downloads.ini" -c "$CACHE"
python3 "$CORE_REPO/utils/downloads.py" unpack -i "$CORE_REPO/downloads.ini" -c "$CACHE" "$SRC"
python3 "$CORE_REPO/utils/patches.py" apply "$SRC" "$CORE_REPO/patches"
python3 "$CORE_REPO/utils/patches.py" apply "$SRC" "$PLATFORM_PATCHES"
python3 "$CORE_REPO/utils/prune_binaries.py" "$SRC" "$CORE_REPO/pruning.list"
# The lite archive omits a Torque source used by Chromium's build graph.
cp -R "$REPO/build/windows/lite-tarball-files/." "$SRC/"
"$REPO/build/apply-patches.sh" "$SRC"

if [ "$PLATFORM" = macos ]; then
  rm -rf "$PLATFORM_REPO/ungoogled-chromium"
  ln -s "$CORE_REPO" "$PLATFORM_REPO/ungoogled-chromium"
  mkdir -p "$PLATFORM_REPO/build"
  rm -rf "$PLATFORM_REPO/build/src" "$PLATFORM_REPO/build/download_cache"
  ln -s "$SRC" "$PLATFORM_REPO/build/src"
  ln -s "$CACHE" "$PLATFORM_REPO/build/download_cache"
  RESOURCE_ARCH="$ARCH"
  [ "$ARCH" = x64 ] && RESOURCE_ARCH=x86_64
  python3 "$PLATFORM_REPO/retrieve_and_unpack_resource.py" -p "$RESOURCE_ARCH"
fi
printf '%s\n' "$CORE_COMMIT" > "$SRC/.chromix-ungoogled-core"
printf '%s\n' "$PLATFORM_COMMIT" > "$SRC/.chromix-ungoogled-platform"
printf '%s\n' "$CHROMIUM_VERSION" > "$SRC/.chromix-chromium-version"
printf '%s\n' "$KEY" > "$READY"
echo "==> prepared ungoogled source: $KEY"
