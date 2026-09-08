"""Test optional cache wiring without downloading or compiling Chromium."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
BASH32 = Path.home() / ".local/bash-3.2-for-ci/bash"


class PosixUpstreamCacheTest(unittest.TestCase):
    def test_all_five_targets_receive_cache_option(self):
        caller = (REPO / ".github/workflows/build-cross-platform.yml").read_text()
        self.assertEqual(caller.count("use_upstream_cache: ${{"), 5)
        self.assertIn("'tools/*upstream_cache.py'", caller)
        workflow = (REPO / ".github/workflows/build-posix-github.yml").read_text()
        self.assertIn("UPSTREAM_ACTIONS_TOKEN:", workflow)
        self.assertIn("secrets.UPSTREAM_ACTIONS_TOKEN || github.token", workflow)
        self.assertIn("upstream-cache-import.json", workflow)
        self.assertIn("upstream-cache-plan.log", workflow)

    def test_fetch_only_after_source_preparation_on_first_stage(self):
        stage = (REPO / "build/posix/ci-stage.sh").read_text()
        self.assertLess(stage.index('"$REPO/build/prepare-ungoogled.sh"'),
                        stage.index('"$REPO/build/posix/fetch-upstream-cache.sh"'))
        self.assertIn('[ "$STAGE_INDEX" -eq 1 ] && [ -z "$FROM_SNAPSHOT" ]', stage)
        self.assertIn('[ "$(remaining_min)" -ge 90 ]', stage)
        self.assertIn('export CHROMIX_UPSTREAM_CACHE_DIR=', stage)

    def test_builders_keep_domain_and_graph_order(self):
        for platform, script in (("linux", "build/build.sh"), ("macos", "build/macos/build.sh")):
            with self.subTest(platform=platform):
                source = (REPO / script).read_text()
                self.assertLess(source.index(f"chromix_import_upstream_cache toolchain {platform}"),
                                source.index('utils/domain_substitution.py" apply'))
                self.assertLess(source.index('utils/domain_substitution.py" apply'),
                                source.index(f"chromix_import_upstream_cache objects {platform}"))
                if platform == "linux":
                    self.assertLess(source.index("chromix_configure_upstream_objects"),
                                    source.index('"$OUT/gn" gen'))
                    self.assertLess(source.index('"$OUT/gn" gen'),
                                    source.index("chromix_import_upstream_cache objects linux"))
                else:
                    self.assertLess(source.index(f"chromix_import_upstream_cache objects {platform}"),
                                    source.index('"$OUT/gn" gen'))
                self.assertLess(source.index('"$OUT/gn" gen'),
                                source.index("chromix_report_upstream_plan"))
                self.assertIn('OUT="$SRC/out/Chromix"', source)

    def run_helper(self, bash, enabled):
        with tempfile.TemporaryDirectory(prefix="cache shell ") as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / "tools").mkdir(parents=True)
            log = root / "args"
            (repo / "tools/import_upstream_cache.py").write_text(
                'import os, sys\nfrom pathlib import Path\n'
                'Path(os.environ["TEST_CACHE_LOG"]).write_text("\\n".join(sys.argv[1:]))\n')
            env = {**os.environ, "REPO": str(repo), "WORK": str(root / "work"),
                   "ARCH": "arm64", "TEST_CACHE_LOG": str(log)}
            env.pop("CHROMIX_UPSTREAM_CACHE_DIR", None)
            if enabled:
                env["CHROMIX_UPSTREAM_CACHE_DIR"] = str(root / "cache with spaces")
            result = subprocess.run(
                [str(bash), "-euo", "pipefail", "-c",
                 'source "$1"; chromix_import_upstream_cache toolchain linux',
                 "fixture", str(REPO / "build/posix/upstream-cache.sh")],
                capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if enabled:
                self.assertEqual(log.read_text().splitlines(), [
                    "--phase", "toolchain", "--platform", "linux", "--arch", "arm64",
                    "--workdir", str(root / "work"), "--cache-dir", str(root / "cache with spaces")])
            else:
                self.assertFalse(log.exists())

    def test_object_wrapper_survives_stage_resume_without_donor_environment(self):
        with tempfile.TemporaryDirectory(prefix="object wrapper ") as directory:
            root = Path(directory)
            out = root / "src/out/Chromix"
            out.mkdir(parents=True)
            args = out / "args.gn"
            (root / "cache").mkdir()
            (root / "cache/result.json").write_text(json.dumps({"status": "hit", "extraction_scope": "source-and-objects"}))
            env = {**os.environ, "REPO": str(root / "repo with spaces"),
                   "WORK": str(root), "OUT": str(out), "CHROMIX_UPSTREAM_CACHE_DIR": str(root / "cache")}
            for resumed in (False, True):
                if resumed:
                    env.pop("CHROMIX_UPSTREAM_CACHE_DIR")
                args.write_text('is_debug = false\n')
                result = subprocess.run(
                    [str(BASH32 if BASH32.exists() else shutil.which("bash")), "-euo", "pipefail", "-c",
                     'source "$1"; chromix_configure_upstream_objects', "fixture",
                     str(REPO / "build/posix/upstream-cache.sh")],
                    env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = args.read_text().splitlines()
                self.assertEqual(lines[0], "is_debug = false")
                command = json.loads(lines[1].split(" = ", 1)[1])
                self.assertEqual(command, "python3 '" + str(root / "repo with spaces/tools/upstream_object_cache.py") + "' compile --")
                self.assertTrue((root / ".chromix-object-wrapper").is_file())

    def test_failed_download_does_not_enable_compiler_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "src/out/Chromix"
            out.mkdir(parents=True)
            (out / "args.gn").write_text('is_debug = false\n')
            result = subprocess.run(
                [shutil.which("bash"), "-euo", "pipefail", "-c",
                 'source "$1"; chromix_configure_upstream_objects', "fixture",
                 str(REPO / "build/posix/upstream-cache.sh")],
                env={**os.environ, "WORK": str(root), "OUT": str(out), "REPO": str(REPO),
                     "CHROMIX_UPSTREAM_CACHE_DIR": str(root / "absent")},
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("cc_wrapper", (out / "args.gn").read_text())
            self.assertFalse((root / ".chromix-object-wrapper").exists())

    def test_donor_finalized_after_build_before_handoff(self):
        source = (REPO / "build/posix/ci-stage.sh").read_text()
        self.assertLess(source.index("RC=$?"), source.index("--phase finalize"))
        self.assertLess(source.index("--phase finalize"), source.index('if [ "$RC" -eq 124 ]'))
        self.assertIn("upstream-object-cache.json", (REPO / ".github/workflows/build-posix-github.yml").read_text())

    def test_disabled_cache_is_noop(self):
        self.run_helper(shutil.which("bash"), False)

    def test_cache_paths_are_quoted(self):
        self.run_helper(shutil.which("bash"), True)

    @unittest.skipUnless(BASH32.exists(), "Bash 3.2 required")
    def test_bash32_enabled_and_disabled(self):
        self.run_helper(BASH32, False)
        self.run_helper(BASH32, True)


if __name__ == "__main__":
    unittest.main()
