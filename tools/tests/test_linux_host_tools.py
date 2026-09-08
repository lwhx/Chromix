"""Execute host-tool setup without compiling Chromium."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]


class LinuxHostToolTest(unittest.TestCase):
    def run_fixture(self, machine, arch):
        temp = tempfile.TemporaryDirectory(prefix="chromix host tools ")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        repo = root / "repo"
        work = root / "work"
        tools = root / "bin"
        (repo / "build").mkdir(parents=True)
        tools.mkdir()
        (work / "src").mkdir(parents=True)
        shutil.copy2(REPO / "build/build.sh", repo / "build/build.sh")
        (repo / "build/posix").mkdir()
        shutil.copy2(REPO / "build/posix/upstream-cache.sh", repo / "build/posix/upstream-cache.sh")
        prepare = repo / "build/prepare-ungoogled.sh"
        prepare.write_text("#!/bin/sh\nexit 0\n")
        prepare.chmod(0o755)
        # Stop immediately after host-tool validation, before any toolchain build.
        (work / "src/.chromix-domain-substituted").touch()
        log = root / "go-invocations"
        for name in ("node", "gperf", "clang-format", "ninja"):
            path = tools / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        uname = tools / "uname"
        uname.write_text(f'#!/bin/sh\ncase "$1" in -m) printf "{machine}\\n";; -s) printf "Linux\\n";; esac\n')
        uname.chmod(0o755)
        go = tools / "go"
        go.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$GO_TEST_LOG"\n')
        go.chmod(0o755)
        result = subprocess.run(
            ["bash", str(repo / "build/build.sh"), str(work), arch],
            env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                 "CHROMIX_SKIP_DEPS": "1", "GO_TEST_LOG": str(log)},
            capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("toolchain is incomplete", result.stderr)
        expected = "amd64" if arch == "x64" else "arm64"
        dawn_go = work / f"src/third_party/dawn/tools/golang/linux-{expected}/bin/go"
        self.assertTrue(dawn_go.is_symlink())
        self.assertEqual(dawn_go.resolve(), go)
        self.assertIn(f"linux-{expected}/bin/go version", log.read_text())
        self.assertFalse((work / "src/third_party/dawn/tools/golang/linux-x64").exists())
        node_link = work / f"src/third_party/node/linux/node-linux-{arch}/bin/node"
        self.assertEqual(node_link.resolve(), tools / "node")

    def test_x64_uses_dawn_amd64_directory(self):
        self.run_fixture("x86_64", "x64")

    def test_arm64_uses_dawn_arm64_directory(self):
        self.run_fixture("aarch64", "arm64")


if __name__ == "__main__":
    unittest.main()
