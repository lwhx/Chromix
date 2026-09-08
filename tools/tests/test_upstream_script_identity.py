import tempfile
from pathlib import Path
import unittest

from tools.upstream_script_identity import ScriptIdentity


class ScriptIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Path(self.temp.name)
        (self.core / "domain_substitution.list").write_text(
            "tools/clang/scripts/build.py\ntools/clang/plugins/test.cc\n")
        (self.core / "domain_regex.list").write_text(
            r"chromium\.googlesource\.com#chromium.9oo91esource.qjz9zk" + "\n" +
            r"www\.google\.com#www.9oo91e.qjz9zk" + "\n")

    def test_linux_restores_only_pinned_download_endpoints(self):
        identity = ScriptIdentity(self.core, "linux")
        raw = b"chromium.googlesource.com www.google.com"
        expected = b"chromium.googlesource.com www.9oo91e.qjz9zk"
        self.assertTrue(identity.matches("tools/clang/scripts/build.py", raw, expected))
        self.assertFalse(identity.matches("tools/clang/scripts/build.py", raw, expected + b" extra code"))
        self.assertEqual(identity.expected("tools/clang/plugins/test.cc", raw),
                         b"chromium.9oo91esource.qjz9zk www.9oo91e.qjz9zk")

    def test_macos_uses_forward_substitution_without_endpoint_restoration(self):
        identity = ScriptIdentity(self.core, "macos")
        raw = b"chromium.googlesource.com"
        self.assertEqual(identity.expected("tools/clang/scripts/build.py", raw),
                         b"chromium.9oo91esource.qjz9zk")
        self.assertFalse(identity.matches("unlisted.py", raw, b"chromium.9oo91esource.qjz9zk"))

    def test_latin1_and_original_bytes(self):
        identity = ScriptIdentity(self.core, "linux")
        raw = b"\xff www.google.com"
        self.assertTrue(identity.matches("tools/clang/plugins/test.cc", raw, b"\xff www.9oo91e.qjz9zk"))
        self.assertTrue(identity.matches("tools/clang/plugins/test.cc", raw, raw))


if __name__ == "__main__":
    unittest.main()
