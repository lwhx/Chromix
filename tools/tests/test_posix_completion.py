"""Exercise stage completion against small ZIP fixtures, not Chromium builds."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("timeout") and shutil.which("unzip"), "GNU timeout and unzip required")
class PosixCompletionTest(unittest.TestCase):
    def complete_fixture(self, platform, corrupt):
        temp = tempfile.TemporaryDirectory(prefix="chromix completion ")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        repo = root / "repo"
        work = root / "work"
        out = work / "src/out/Chromix"
        out.mkdir(parents=True)
        (work / "src/.chromix-source-ready").touch()
        (repo / "build/posix").mkdir(parents=True)
        shutil.copy2(REPO / "build/posix/ci-stage.sh", repo / "build/posix/ci-stage.sh")
        version = (REPO / "CHROMIUM_VERSION").read_text().strip()
        (repo / "CHROMIUM_VERSION").write_text(version + "\n")
        if platform == "linux":
            (out / "chrome").touch()
            (out / "chrome").chmod(0o755)
            build = repo / "build/build.sh"
            package = repo / "build/linux/package-linux.sh"
            asset = "chromix-linux-x64.zip"
        else:
            (out / "Chromium.app").mkdir()
            build = repo / "build/macos/build.sh"
            package = repo / "build/macos/package-macos.sh"
            asset = "chromix-mac-x64.zip"
        build.parent.mkdir(parents=True, exist_ok=True)
        build.write_text("#!/bin/sh\nexit 0\n")
        build.chmod(0o755)
        seed = root / "seed"
        seed.mkdir()
        launch_log = root / "launcher-log"
        launcher = ('#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCH_TEST_LOG"\n'
                    f'case "$1" in --version) printf "Chromix {version}\\n";; '
                    '*) printf "<p>chromix-smoke-ok</p>\\n";; esac\n')
        info = zipfile.ZipInfo("chromix/chromix")
        info.create_system = 3
        info.external_attr = 0o100755 << 16
        with zipfile.ZipFile(seed / asset, "w") as archive:
            archive.writestr(info, launcher)
        checksum = hashlib.sha256((seed / asset).read_bytes()).hexdigest()
        if corrupt:
            checksum = "0" * 64
        (seed / "SHA256SUMS").write_text(f"{checksum}  {asset}\n")
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_text('#!/bin/sh\ncp "$TEST_SEED"/* "$2/"\n')
        package.chmod(0o755)
        github_output = root / "github_output"
        result = subprocess.run(
            ["bash", str(repo / "build/posix/ci-stage.sh"), "--platform", platform,
             "--arch", "x64", "--workdir", str(work)],
            env={**os.environ, "TEST_SEED": str(seed), "LAUNCH_TEST_LOG": str(launch_log),
                 "GITHUB_OUTPUT": str(github_output)}, capture_output=True, text=True, timeout=20)
        output = dict(line.split("=", 1) for line in github_output.read_text().splitlines())
        if corrupt:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bundle checksum verification failed", result.stderr)
            self.assertEqual(output["finished"], "false")
            self.assertFalse(launch_log.exists())
            self.assertFalse((work / "smoke").exists())
        else:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(output["finished"], "true")
            self.assertEqual(output["status"], "completed")
            calls = launch_log.read_text()
            self.assertIn("--version", calls)
            self.assertIn("--headless", calls)
            self.assertFalse((work / "smoke").exists())

    def test_linux_valid_bundle_runs_both_smoke_checks(self):
        self.complete_fixture("linux", False)

    def test_linux_corrupted_bundle_never_launches(self):
        self.complete_fixture("linux", True)

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_valid_bundle_runs_both_smoke_checks(self):
        self.complete_fixture("macos", False)

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_macos_corrupted_bundle_never_launches(self):
        self.complete_fixture("macos", True)


if __name__ == "__main__":
    unittest.main()
