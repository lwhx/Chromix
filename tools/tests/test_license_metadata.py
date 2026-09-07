import json
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
LICENSE = REPO / "LICENSE"
PYTHON_LICENSE = REPO / "sdk" / "python" / "LICENSE"
NODE_LICENSE = REPO / "sdk" / "node" / "LICENSE"
PYPROJECT = REPO / "sdk" / "python" / "pyproject.toml"
NODE_PACKAGE = REPO / "sdk" / "node" / "package.json"
README = REPO / "README.md"
PYTHON_README = REPO / "sdk" / "python" / "README.md"
NODE_README = REPO / "sdk" / "node" / "README.md"
PACKAGE_WIN = REPO / "build" / "windows" / "package-win.ps1"
PACKAGE_LINUX = REPO / "build" / "linux" / "package-linux.sh"
PACKAGE_MACOS = REPO / "build" / "macos" / "package-macos.sh"


class LicenseMetadataRegressionTest(unittest.TestCase):
    def test_bsd_three_clause_text_is_present_and_copied_into_sdks(self):
        license_text = LICENSE.read_text(encoding="utf-8")
        self.assertIn("BSD 3-Clause License", license_text)
        self.assertIn("Copyright (c) 2026, xiaozhou26", license_text)
        self.assertIn("Redistributions in binary form", license_text)
        self.assertIn('THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"', license_text)
        self.assertEqual(PYTHON_LICENSE.read_text(encoding="utf-8"), license_text)
        self.assertEqual(NODE_LICENSE.read_text(encoding="utf-8"), license_text)

    def test_python_package_uses_the_license_file(self):
        pyproject = PYPROJECT.read_text(encoding="utf-8")
        self.assertIn('license = { file = "LICENSE" }', pyproject)
        self.assertIn('"License :: OSI Approved :: BSD License"', pyproject)
        self.assertIn('"Operating System :: POSIX :: Linux"', pyproject)
        self.assertIn('"Operating System :: MacOS :: MacOS X"', pyproject)
        self.assertIn('Issues = "https://github.com/xiaozhou26/Chromix/issues"', pyproject)

    def test_node_package_includes_license_and_repository_metadata(self):
        package = json.loads(NODE_PACKAGE.read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "@xiaoxiaofeihh/chromix")
        self.assertEqual(package["version"], "0.1.0")
        self.assertEqual(package["publishConfig"]["access"], "public")
        self.assertEqual(package["license"], "BSD-3-Clause")
        self.assertIn("LICENSE", package["files"])
        self.assertEqual(package["author"], "xiaozhou26")
        self.assertEqual(package["bugs"]["url"], "https://github.com/xiaozhou26/Chromix/issues")

    def test_browser_packages_keep_chromix_and_chromium_licenses(self):
        windows = PACKAGE_WIN.read_text(encoding="utf-8")
        linux = PACKAGE_LINUX.read_text(encoding="utf-8")
        macos = PACKAGE_MACOS.read_text(encoding="utf-8")
        for source in (windows, linux, macos):
            self.assertIn("LICENSE.chromix", source)
            self.assertIn("LICENSE.chromium", source)
        self.assertIn("Chromium license is missing", windows)
        self.assertIn("Chromium license is missing", linux)
        self.assertIn("Chromium license is missing", macos)

    def test_docs_state_license_and_registry_status(self):
        readme = README.read_text(encoding="utf-8")
        python_readme = PYTHON_README.read_text(encoding="utf-8")
        node_readme = NODE_README.read_text(encoding="utf-8")
        normalized_readme = " ".join(readme.split())
        normalized_python_readme = " ".join(python_readme.split())
        normalized_node_readme = " ".join(node_readme.split())
        self.assertIn("## License", readme)
        self.assertIn("BSD 3-Clause License", readme)
        self.assertIn("pip install chromix playwright", normalized_readme)
        self.assertIn("npm install @xiaoxiaofeihh/chromix playwright-core", normalized_readme)
        self.assertIn("pip install chromix playwright", normalized_python_readme)
        self.assertIn("@xiaoxiaofeihh/chromix", normalized_node_readme)
        self.assertIn("unrelated project", normalized_node_readme)
        self.assertNotIn("npm install chromix playwright-core", node_readme)


if __name__ == "__main__":
    unittest.main()
