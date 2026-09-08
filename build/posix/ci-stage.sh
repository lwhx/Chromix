#!/usr/bin/env bash
# One stage of the GitHub-hosted POSIX (Linux/macOS) build, modeled on
# ungoogled-chromium-portablelinux's prep/build_part_NN CI and
# ungoogled-chromium-macos' retrieve-resources/build_job_NN chain: each job
# restores the previous stage's tar|zstd tree snapshot when one exists,
# prepares the pinned source or resumes ninja under `timeout -k`, snapshots
# the work tree again (mtimes/modes/symlinks preserved for ninja state), and
# reports status=running|completed through GITHUB_OUTPUT. A failed compile
# fails the job; only the deadline path hands off to the next stage.
#
# Usage:
#   ci-stage.sh --platform linux|macos --arch x64|arm64 \
#     [--workdir DIR] [--stage-index N] [--max-stages N]
#     [--from-snapshot DIR] [--deadline-epoch EPOCH] [--reserve-minutes N]
set -euo pipefail

# Resolve to the repository root: this script lives at <repo>/build/posix/,
# so two parent hops are needed, not one.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLATFORM=""
ARCH=""
WORK="${CHROMIX_WORKDIR:-$REPO/.chromix-build-posix}"
STAGE_INDEX=1
MAX_STAGES=8
FROM_SNAPSHOT=""
DEADLINE_EPOCH=0
RESERVE_MINUTES="${CHROMIX_RESERVE_MINUTES:-45}"

while [ $# -gt 0 ]; do
  case "$1" in
    --platform) PLATFORM="${2:?--platform needs a value}"; shift 2 ;;
    --arch) ARCH="${2:?--arch needs a value}"; shift 2 ;;
    --workdir) WORK="${2:?--workdir needs a value}"; shift 2 ;;
    --stage-index) STAGE_INDEX="${2:?--stage-index needs a value}"; shift 2 ;;
    --max-stages) MAX_STAGES="${2:?--max-stages needs a value}"; shift 2 ;;
    --from-snapshot) FROM_SNAPSHOT="${2:?--from-snapshot needs a value}"; shift 2 ;;
    --deadline-epoch) DEADLINE_EPOCH="${2:?--deadline-epoch needs a value}"; shift 2 ;;
    --reserve-minutes) RESERVE_MINUTES="${2:?--reserve-minutes needs a value}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$PLATFORM" in linux|macos) ;; *) echo "--platform linux|macos is required" >&2; exit 2 ;; esac
case "$ARCH" in x64|arm64) ;; *) echo "--arch x64|arm64 is required" >&2; exit 2 ;; esac

mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
SRC="$WORK/src"
OUT="$SRC/out/Chromix"
SNAPSHOT_DIR="$WORK/.snapshot-stage-$STAGE_INDEX"

emit() {
  # A bare `[ ... ] && printf` under `set -e` returns nonzero and aborts
  # when GITHUB_OUTPUT is unset (local runs); branch explicitly instead.
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf '%s=%s\n' "$1" "$2" >>"$GITHUB_OUTPUT"
  fi
}
log() { printf '==> %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
now_epoch() { date +%s; }

remaining_min() {
  if [ "${DEADLINE_EPOCH:-0}" -le 0 ]; then echo 300; return; fi
  # macOS runners execute workflow steps with the system /bin/bash 3.2,
  # whose $(( )) cannot nest a quoted command substitution; expand to a
  # variable first (verified against a locally built 3.2.0).
  local now left
  now="$(now_epoch)"
  left=$(( (DEADLINE_EPOCH - now) / 60 ))
  [ "$left" -lt 0 ] && left=0
  echo "$left"
}

emit status running
emit finished false

# ---- restore previous stage snapshot ------------------------------------
if [ -n "$FROM_SNAPSHOT" ] && [ ! -d "$FROM_SNAPSHOT" ] && [ "$STAGE_INDEX" -gt 1 ]; then
  die "resume snapshot directory does not exist: $FROM_SNAPSHOT"
fi
if [ -n "$FROM_SNAPSHOT" ] && [ -d "$FROM_SNAPSHOT" ]; then
  command -v zstd >/dev/null 2>&1 || die "zstd is required to restore a POSIX build snapshot"
  find "$FROM_SNAPSHOT" -name 'tree.tar.zst*' -print -quit | grep -q . ||
    die "snapshot has no tree archive: $FROM_SNAPSHOT"
  find "$FROM_SNAPSHOT" -name 'tree.tar.zst*' -print0 | sort -z |
    xargs -0 cat | zstd -d -T0 | tar -xpf - -C "$WORK"
  rm -rf "$FROM_SNAPSHOT"
fi

# Own snapshots take precedence over a fresh upstream restore.
if [ "$STAGE_INDEX" -eq 1 ] && [ -z "$FROM_SNAPSHOT" ] &&
   [ ! -e "$SRC" ] && [ "${CHROMIX_USE_UPSTREAM_CACHE:-0}" = 1 ] &&
   [ "$(remaining_min)" -ge 90 ]; then
  UPSTREAM_CACHE_DIR="${RUNNER_TEMP:-$(dirname "$WORK")}/chromix-upstream"
  bash "$REPO/build/posix/fetch-upstream-cache.sh" \
    --platform "$PLATFORM" --arch "$ARCH" --destination "$UPSTREAM_CACHE_DIR"
  python3 "$REPO/tools/restore_upstream_cache.py" --phase restore \
    --platform "$PLATFORM" --arch "$ARCH" --workdir "$WORK" \
    --cache-dir "$UPSTREAM_CACHE_DIR"
fi
if [ -f "$SRC/.chromix-upstream-restored.json" ]; then
  OUT="$SRC/out/Default"
fi

if [ -f "$SRC/.chromix-domain-substitution-in-progress" ]; then
  die "domain substitution was interrupted; use a clean work directory"
fi
if [ -f "$SRC/.chromix-source-ready" ]; then
  # prepare-ungoogled.sh revalidates version pins, commits, and patch hash on rerun.
  :
else
  PREPARE_BUDGET=$(( $(remaining_min) - RESERVE_MINUTES ))
  if [ "$PREPARE_BUDGET" -lt 25 ]; then
    log "stage $STAGE_INDEX: preparation budget ${PREPARE_BUDGET}m below minimum; handing off"
    mkdir -p "$SNAPSHOT_DIR"
    bash "$REPO/build/posix/ci-parts.sh" "$WORK" "$SNAPSHOT_DIR"
    emit upload_snapshot true
    exit 0
  fi
  "$REPO/build/prepare-ungoogled.sh" "$WORK" "$PLATFORM" "$ARCH"
fi

# ---- bounded compile ------------------------------------------------------
NINJA_BUDGET=$(( $(remaining_min) - RESERVE_MINUTES ))
if [ "$NINJA_BUDGET" -le 20 ]; then
  log "stage $STAGE_INDEX: ninja budget ${NINJA_BUDGET}m below minimum; handing off"
  mkdir -p "$SNAPSHOT_DIR"
  bash "$REPO/build/posix/ci-parts.sh" "$WORK" "$SNAPSHOT_DIR"
  emit upload_snapshot true
  exit 0
fi

if [ "$PLATFORM" = linux ]; then
  BUILD_SCRIPT="$REPO/build/build.sh"
else
  BUILD_SCRIPT="$REPO/build/macos/build.sh"
fi

# GNU timeout is not shipped by macOS; Homebrew's coreutils provides it as
# gtimeout with identical semantics (-k/-s), mirroring the gsplit fallback
# in ci-parts.sh.
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT=timeout
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT=gtimeout
else
  die "neither timeout nor gtimeout is available"
fi

set +e
"$TIMEOUT" -k 7m -s SIGTERM "${NINJA_BUDGET}m" \
  env CHROMIX_SKIP_DEPS=1 CHROMIX_WORKDIR="$WORK" \
    "$BUILD_SCRIPT" "$WORK" "$ARCH"
RC=$?
set -e
# Mirror the Windows chain's last-stage guard: a green run without a finished
# build would let release-browser accept an incomplete artifact set.

if [ "$RC" -eq 124 ]; then
  if [ "$STAGE_INDEX" -ge "$MAX_STAGES" ]; then
    die "stage $STAGE_INDEX reached max-stages $MAX_STAGES without finishing"
  fi
  log "stage $STAGE_INDEX: deadline reached after ${NINJA_BUDGET}m; handing off"
  mkdir -p "$SNAPSHOT_DIR"
  bash "$REPO/build/posix/ci-parts.sh" "$WORK" "$SNAPSHOT_DIR"
  emit upload_snapshot true
  exit 0
elif [ "$RC" -ne 0 ]; then
  die "build script failed at stage $STAGE_INDEX (exit $RC)"
fi

# ---- completion checks -----------------------------------------------------
if [ "$PLATFORM" = linux ]; then
  [ -x "$OUT/chrome" ] || die "build exited 0 but $OUT/chrome is missing"
else
  [ -d "$OUT/Chromium.app" ] || die "build exited 0 but $OUT/Chromium.app is missing"
fi

DEST_DIST="$WORK/dist"
rm -rf "$DEST_DIST"
mkdir -p "$DEST_DIST"
if [ "$PLATFORM" = linux ]; then
  bash "$REPO/build/linux/package-linux.sh" "$OUT" "$DEST_DIST" "$ARCH"
else
  bash "$REPO/build/macos/package-macos.sh" "$OUT/Chromium.app" "$DEST_DIST" "$ARCH"
fi

cd "$DEST_DIST"
ASSET="chromix-linux-$ARCH.zip"
[ "$PLATFORM" = macos ] && ASSET="chromix-mac-$ARCH.zip"
[ -s "$ASSET" ] || die "packaging did not produce $ASSET in $DEST_DIST"
grep -E "^[0-9a-fA-F]{64}  $ASSET\$" SHA256SUMS >/dev/null ||
  die "SHA256SUMS is missing the entry for $ASSET"
if [ "$PLATFORM" = linux ]; then
  sha256sum -c SHA256SUMS || die "bundle checksum verification failed"
else
  shasum -a 256 -c SHA256SUMS || die "bundle checksum verification failed"
fi

SMOKE_DIR="$WORK/smoke"
rm -rf "$SMOKE_DIR"
mkdir -p "$SMOKE_DIR"
unzip -q "$DEST_DIST/$ASSET" -d "$SMOKE_DIR"
LAUNCHER="$SMOKE_DIR/chromix/chromix"
[ -x "$LAUNCHER" ] || die "extracted bundle launcher is missing: $LAUNCHER"

VERSION_OUTPUT="$("$TIMEOUT" 30s "$LAUNCHER" --version)" ||
  die "extracted launcher --version check failed"
echo "$VERSION_OUTPUT"
CHROMIUM_VERSION_PIN="$(tr -d '\n' < "$REPO/CHROMIUM_VERSION")"
grep -qF "$CHROMIUM_VERSION_PIN" <<<"$VERSION_OUTPUT" ||
  die "extracted browser version does not match the pinned Chromium version"

DOM_OUTPUT="$("$TIMEOUT" 60s "$LAUNCHER" --headless --disable-gpu --no-first-run \
  --no-default-browser-check "--user-data-dir=$SMOKE_DIR/profile" \
  --dump-dom 'data:text/html,<p>chromix-smoke-ok</p>')" ||
  die "extracted headless smoke test failed"
echo "$DOM_OUTPUT"
grep -qF '<p>chromix-smoke-ok</p>' <<<"$DOM_OUTPUT" ||
  die "smoke page marker missing from dumped DOM"

rm -rf "$SMOKE_DIR"
emit finished true
emit status completed
exit 0
