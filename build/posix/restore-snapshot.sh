#!/usr/bin/env bash
# Share the same transactional restore between same-run and cross-run jobs.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 "$REPO/tools/restore_posix_snapshot.py" \
  "${1:?usage: restore-snapshot.sh SNAPSHOT_DIR DEST_DIR}" \
  "${2:?usage: restore-snapshot.sh SNAPSHOT_DIR DEST_DIR}"
