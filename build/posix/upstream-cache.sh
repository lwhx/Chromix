#!/usr/bin/env bash
# Optional verified donors never replace the canonical prepared source.
chromix_import_upstream_cache() {
  local phase="$1" platform="$2"
  if [ -n "${CHROMIX_UPSTREAM_CACHE_DIR:-}" ]; then
    python3 "$REPO/tools/import_upstream_cache.py" \
      --phase "$phase" --platform "$platform" --arch "$ARCH" \
      --workdir "$WORK" --cache-dir "$CHROMIX_UPSTREAM_CACHE_DIR"
  fi
}

chromix_has_upstream_toolchain() {
  [ -n "${CHROMIX_UPSTREAM_CACHE_DIR:-}" ] || return 1
  python3 - "$WORK/upstream-cache-import.json" <<'PY'
import json
import sys
from pathlib import Path
try:
    phase = json.loads(Path(sys.argv[1]).read_text())["phases"]["toolchain"]
    reused = phase.get("reused", {})
    valid = phase["status"] == "hit" and reused.get("clang") and reused.get("rust")
except (OSError, ValueError, KeyError, TypeError):
    valid = False
sys.exit(0 if valid else 1)
PY
}

chromix_configure_upstream_objects() {
  if [ -n "${CHROMIX_UPSTREAM_CACHE_DIR:-}" ] || [ -f "$WORK/.chromix-object-wrapper" ]; then
    python3 - "$OUT/args.gn" "$REPO/tools/upstream_object_cache.py" "$WORK" "${CHROMIX_UPSTREAM_CACHE_DIR:-}" <<'PY'
import json
from pathlib import Path
import shlex
import sys
path = Path(sys.argv[1])
marker = Path(sys.argv[3]) / ".chromix-object-wrapper"
if not marker.exists():
    try:
        result = json.loads((Path(sys.argv[4]) / "result.json").read_text())
        if result.get("status") != "hit" or result.get("extraction_scope") != "source-and-objects":
            sys.exit(0)
    except (OSError, ValueError, AttributeError):
        sys.exit(0)
command = "python3 " + shlex.quote(sys.argv[2]) + " compile --"
with path.open("a", encoding="utf-8") as output:
    output.write("cc_wrapper = " + json.dumps(command) + "\n")
marker.touch()
PY
  fi
}

chromix_report_upstream_plan() {
  if [ -n "${CHROMIX_UPSTREAM_CACHE_DIR:-}" ] || [ -f "$WORK/src/.chromix-upstream-restored.json" ]; then
    # A dry run records planned work; it cannot establish elapsed-time savings.
    ninja -C "$OUT" -n "$@" > "$WORK/upstream-cache-plan.log" 2>&1
  fi
}
