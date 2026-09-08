"""A stale marker cannot cause Linux to skip compiler preparation."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]


class UpstreamToolchainGateTest(unittest.TestCase):
    def gate(self, report, enabled=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "upstream-cache-import.json").write_text(json.dumps(report))
            env = {**os.environ, "WORK": str(root)}
            env.pop("CHROMIX_UPSTREAM_CACHE_DIR", None)
            if enabled:
                env["CHROMIX_UPSTREAM_CACHE_DIR"] = str(root / "cache")
            result = subprocess.run(
                ["bash", "-eu", "-c", 'source "$1"; chromix_has_upstream_toolchain',
                 "fixture", str(REPO / "build/posix/upstream-cache.sh")],
                env=env, capture_output=True, text=True, timeout=10)
            return result.returncode

    def test_only_validated_complete_toolchain_hit_skips_bootstrap(self):
        entry = {"status": "hit", "reused": {"clang": True, "rust": True}}
        self.assertEqual(self.gate({"phases": {"toolchain": entry}}), 0)
        for invalid in ({}, {"status": "miss"}, {"status": "hit", "reused": {"bindgen": True}},
                        {"status": "hit", "reused": {"clang": True}}):
            self.assertNotEqual(self.gate({"phases": {"toolchain": invalid}}), 0)
        self.assertNotEqual(self.gate({"phases": {"toolchain": entry}}, enabled=False), 0)


if __name__ == "__main__":
    unittest.main()
