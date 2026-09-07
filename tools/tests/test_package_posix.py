"""Exercise the packaging scripts with miniature native bundle layouts."""
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "sdk/python"))
from chromix._binary import _extract_zip


@unittest.skipUnless(shutil.which("zip") and os.name == "posix", "POSIX zip required")
class PosixPackageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromix package ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / "src"
        self.out = self.src / "out/Chromix"
        self.out.mkdir(parents=True)
        (self.src / "LICENSE").write_text("Chromium fixture license")
        self.fonts = self.root / "fonts"
        self.fonts.mkdir()
        for name in ("font.ttf", "font.ttc", "fonts.conf.template", "NOTICE", "SOURCE.md"):
            (self.fonts / name).write_text("fixture")
        self.env = {**os.environ, "CHROMIX_FONTS_DIR": str(self.fonts)}

    def run_package(self, platform, source, arch, success=True, dest=None):
        dest = dest or self.root / (platform + "-" + arch)
        result = subprocess.run(
            ["bash", str(REPO / f"build/{platform}/package-{platform}.sh"),
             str(source), str(dest), arch], env=self.env, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return dest, result

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nprintf "fixture:%s\\n" "$*"\n')
        path.chmod(0o755)

    def verify_bundle(self, dest, asset, binary):
        archive = dest / asset
        expected = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertIn(f"{expected}  {asset}", (dest / "SHA256SUMS").read_text())
        extracted = dest / "extracted"
        extracted.mkdir()
        _extract_zip(archive, extracted)
        bundle = extracted / "chromix"
        self.assertEqual((bundle / "LICENSE.chromium").read_text(), "Chromium fixture license")
        self.assertTrue((bundle / "LICENSE.chromix").is_file())
        self.assertTrue((bundle / "fonts/font.ttc").is_file())
        self.assertTrue((bundle / binary).stat().st_mode & stat.S_IXUSR)
        result = subprocess.run([str(bundle / "chromix"), "--version"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "fixture:--version")
        return bundle

    def linux_layout(self):
        for name in ("chrome", "chrome_crashpad_handler", "chrome_sandbox"):
            self.executable(self.out / name)
        for name in ("icudtl.dat", "resources.pak", "v8_context_snapshot.bin"):
            (self.out / name).write_text("fixture")
        (self.out / "locales").mkdir()
        (self.out / "locales/en-US.pak").write_text("fixture")

    def mac_layout(self):
        app = self.out / "Chromium.app"
        self.executable(app / "Contents/MacOS/Chromium")
        framework = app / "Contents/Frameworks/Chromium Framework.framework"
        self.executable(framework / "Versions/1/Helpers/helper")
        (framework / "Versions/Current").symlink_to("1")
        (framework / "Helpers").symlink_to("Versions/Current/Helpers")
        return app

    def test_linux_both_architectures_and_missing_runtime(self):
        self.linux_layout()
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                dest, _ = self.run_package("linux", self.out, arch)
                bundle = self.verify_bundle(dest, f"chromix-linux-{arch}.zip", "chrome")
                self.assertTrue((bundle / "chrome-sandbox").stat().st_mode & stat.S_IXUSR)
        (self.out / "icudtl.dat").unlink()
        _, result = self.run_package("linux", self.out, "x64", success=False)
        self.assertIn("required runtime file", result.stderr)

    def test_macos_both_architectures_symlinks_and_default_license(self):
        app = self.mac_layout()
        for arch in ("x86_64", "arm64"):
            with self.subTest(arch=arch):
                dest, _ = self.run_package("macos", app, arch)
                normalized = "x64" if arch == "x86_64" else arch
                bundle = self.verify_bundle(dest, f"chromix-mac-{normalized}.zip",
                                            "Chromium.app/Contents/MacOS/Chromium")
                helpers = bundle / "Chromium.app/Contents/Frameworks/Chromium Framework.framework/Helpers"
                self.assertTrue(helpers.is_symlink())
                self.assertTrue((helpers / "helper").stat().st_mode & stat.S_IXUSR)
        (self.src / "LICENSE").unlink()
        _, result = self.run_package("macos", app, "x64", success=False)
        self.assertIn("Chromium license is missing", result.stderr)

    def test_linux_rejects_empty_locales(self):
        self.linux_layout()
        (self.out / "locales/en-US.pak").unlink()
        _, result = self.run_package("linux", self.out, "x64", success=False)
        self.assertIn("no locale resource packs", result.stderr)

    def test_shared_destination_keeps_all_checksums(self):
        self.linux_layout()
        app = self.mac_layout()
        dest = self.root / "dist"
        assets = []
        for platform, source in (("linux", self.out), ("macos", app)):
            for arch in ("x64", "arm64"):
                self.run_package(platform, source, arch, dest=dest)
                prefix = "mac" if platform == "macos" else platform
                assets.append(f"chromix-{prefix}-{arch}.zip")
        lines = (dest / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(lines), 4)
        for asset in assets:
            digest = hashlib.sha256((dest / asset).read_bytes()).hexdigest()
            self.assertIn(f"{digest}  {asset}", lines)

    def test_macos_rejects_unknown_architecture(self):
        _, result = self.run_package("macos", self.mac_layout(), "ppc", success=False)
        self.assertIn("unsupported macOS package architecture", result.stderr)


if __name__ == "__main__":
    unittest.main()
