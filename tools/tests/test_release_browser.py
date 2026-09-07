import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.release_browser import parse_manifest, validate_bundle


class ReleaseBrowserTest(unittest.TestCase):
    def test_parse_manifest_accepts_only_named_browser_assets(self):
        digest = "a" * 64
        parsed = parse_manifest(f"{digest}  chromix-linux-x64.zip\n")
        self.assertEqual(parsed, {"chromix-linux-x64.zip": digest})

    def test_parse_manifest_rejects_unknown_or_duplicate_assets(self):
        with self.assertRaises(ValueError):
            parse_manifest(f"{'a' * 64}  unrelated.zip\n")
        with self.assertRaises(ValueError):
            parse_manifest(f"{'a' * 64}  chromix-linux-x64.zip\n{'b' * 64}  chromix-linux-x64.zip\n")

    def test_validate_bundle_checks_layout_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chromix-linux-x64.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name in (
                    "chromix/chromix", "chromix/chrome", "chromix/LICENSE.chromix",
                    "chromix/LICENSE.chromium",
                ):
                    archive.writestr(name, "fixture")
            validate_bundle(path)
            bad = Path(directory) / "bad.zip"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("chromix/../outside", "bad")
            with self.assertRaises(ValueError):
                validate_bundle(bad)


if __name__ == "__main__":
    unittest.main()
