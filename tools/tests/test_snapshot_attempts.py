"""Every same-run handoff uses the producer step output, not the consumer attempt."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]

class SnapshotAttemptTest(unittest.TestCase):
    def test_all_five_platforms_use_producer_attempt(self):
        for filename, prefix, count in (("build-win-x64-github.yml", "build", 12),
                                        ("build-posix-github.yml", "posix", 8)):
            jobs = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())["jobs"]
            for n in range(1, count + 1):
                with self.subTest(workflow=filename, stage=n):
                    job = jobs[f"{prefix}-{n}"]
                    self.assertEqual(job["outputs"]["snapshot_attempt"],
                                     "${{ steps.snapshot_origin.outputs.attempt }}")
                    producer = next(s for s in job["steps"] if s.get("id") == "snapshot_origin")
                    self.assertIn("GITHUB_RUN_ATTEMPT", producer["run"])
                    if n == 1:
                        continue
                    downloads = [s for s in job["steps"] if s.get("uses") == "actions/download-artifact@v4"
                                 and "run-id" not in s.get("with", {})]
                    self.assertEqual(len(downloads), 1)
                    pattern = downloads[0]["with"]["pattern"]
                    self.assertIn("${{ needs." + f"{prefix}-{n-1}" + ".outputs.snapshot_attempt }}", pattern)
                    self.assertNotIn("attempt-*", pattern)
                    self.assertNotIn("github.run_attempt", pattern)
