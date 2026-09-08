#!/usr/bin/env bash
# Bound optional downloads without turning local errors into cache misses.
set -euo pipefail
set +m

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
die() { printf 'upstream cache: %s\n' "$*" >&2; exit 2; }
SECONDS_LIMIT="${CHROMIX_CACHE_TIMEOUT_SECONDS-1200}"
[[ "$SECONDS_LIMIT" =~ ^[1-9][0-9]*$ ]] || die "CHROMIX_CACHE_TIMEOUT_SECONDS must be a positive integer"
TIMEOUT="$(type -P timeout || type -P gtimeout || true)"
[ -n "$TIMEOUT" ] && [ -x "$TIMEOUT" ] || die "timeout or gtimeout executable is required"
PYTHON="$(type -P python3 || true)"
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || die "python3 executable is required"
[ -f "$REPO/tools/fetch_upstream_cache.py" ] || die "fetch_upstream_cache.py is required"

[ "$#" -gt 0 ] || die "--platform, --arch, and --destination are required"
ARGS=("$@")
DESTINATION=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --platform|--arch|--destination|--run-id)
      [ "$#" -ge 2 ] || die "$1 needs a value"
      if [ "$1" = --destination ]; then DESTINATION="$2"; fi
      shift 2 ;;
    --destination=*) DESTINATION="${1#*=}"; shift ;;
    *) shift ;;
  esac
done

# A non-foreground timeout owns a process group, including the decompressor.
"$TIMEOUT" -k 30s "${SECONDS_LIMIT}s" "$PYTHON" "$REPO/tools/fetch_upstream_cache.py" "${ARGS[@]}" &
TIMEOUT_PID=$!
STATUS=0
wait "$TIMEOUT_PID" || STATUS=$?
case "$STATUS" in
  0) exit 0 ;;
  124|137) ;;
  *) exit "$STATUS" ;;
esac
# The fetcher may exit on TERM before a child does; kill survivors before unlocking.
kill -KILL -- "-$TIMEOUT_PID" 2>/dev/null || true

"$PYTHON" - "$DESTINATION" "$REPO" <<'PY'
import json
import os
from pathlib import Path
import shutil
import stat
import sys


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


try:
    value = sys.argv[1]
    require(bool(value.strip()), "missing destination")
    destination = Path(os.path.abspath(Path(value).expanduser()))
    repo = Path(sys.argv[2])
    require(destination != repo and destination not in repo.parents
            and destination != Path.home(), "destination is not dedicated")
    for path in (destination, *destination.parents):
        require(stat.S_ISDIR(path.lstat().st_mode), "destination contains a symlink or non-directory")
    children = {path.name for path in destination.iterdir()}
    allowed = {"result.json", "tree", ".inner", ".download.zip", ".result.tmp", ".lock"}
    require("result.json" in children and children <= allowed, "unexpected or unowned destination contents")
    for name in children:
        mode = (destination / name).lstat().st_mode
        require(stat.S_ISDIR(mode) if name == "tree" else stat.S_ISREG(mode),
                "invalid destination child: " + name)
    result = json.loads((destination / "result.json").read_text(encoding="utf-8"))
    require(isinstance(result, dict) and result.get("owner") == "chromix-upstream-cache-v1"
            and result.get("destination") == str(destination), "invalid ownership result")
except (OSError, ValueError, UnicodeError) as exc:
    print(f"upstream cache: timeout cleanup refused: {exc}", file=sys.stderr)
    sys.exit(2)

try:
    if "tree" in children:
        shutil.rmtree(destination / "tree")
    for name in (".inner", ".download.zip", ".result.tmp"):
        if name in children:
            (destination / name).unlink()
    result.update(status="miss", reason="cache_timeout", source=None)
    temporary = destination / ".result.tmp"
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
    temporary.replace(destination / "result.json")
    if ".lock" in children:
        (destination / ".lock").unlink()
except OSError as exc:
    print(f"upstream cache: timeout cleanup failed: {exc}", file=sys.stderr)
    sys.exit(2)
print(json.dumps(result, sort_keys=True))
print("upstream cache: miss; cache_timeout", file=sys.stderr)
PY
