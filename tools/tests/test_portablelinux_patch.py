"""Exercise the pinned Linux patch repair with GNU patch, not Chromium builds."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
PREPARE = REPO / "build/prepare-ungoogled.sh"
PATCH_REL = "ungoogled-chromium/portablelinux/fix-compiling-on-arm64.patch"

# Verbatim from ungoogled-chromium-portablelinux 02c59ed68d1963a647bb478064823d114e466ffb.
PINNED_PATCH = '''\
--- a/build/toolchain/linux/BUILD.gn
+++ b/build/toolchain/linux/BUILD.gn
@@ -179,6 +179,13 @@ clang_v8_toolchain("clang_x64_v8_loong64
   }
 }
\x20
+clang_v8_toolchain("clang_arm64_v8_x64") {
+  toolchain_args = {
+    current_cpu = "arm64"
+    v8_current_cpu = "x64"
+  }
+}
+
 gcc_toolchain("x64") {
   cc = "gcc"
   cxx = "g++"
--- a/tools/rust/build_rust.py
+++ b/tools/rust/build_rust.py
@@ -55,7 +55,7 @@ sys.path.append(
                  'scripts'))
\x20
 from build import (AddCMakeToPath, AddZlibToPath, CheckoutGitRepo, CopyFile,
                   DownloadDebianSysroot, FetchUrl, GetLibXml2Dirs,
-                  GitCherryPick, GitRevert, LLVM_DIR, IsGitAncestorToHead,
+                  GetHostSysrootPlatform, GitRevert, LLVM_DIR, IsGitAncestorToHead,
                   LLVM_BUILD_TOOLS_DIR, RunCommand,
                   DEFAULT_MACOSX_DEPLOYMENT_TARGET, GetLatestCommit)
 from update import (CHROMIUM_DIR, DownloadAndUnpack, EnsureDirExists,
@@ -161,8 +161,8 @@ def AddOpenSSLToEnv():
         ssl_url = (f'{CIPD_DOWNLOAD_URL}/{OPENSSL_CIPD_WIN_AMD_PATH}'
                    f'/+/version:2@{OPENSSL_CIPD_WIN_AMD_VERSION}')
     else:
-        ssl_url = (f'{CIPD_DOWNLOAD_URL}/{OPENSSL_CIPD_LINUX_AMD_PATH}'
-                   f'/+/version:2@{OPENSSL_CIPD_LINUX_AMD_VERSION}')
+            ssl_url = (f'{CIPD_DOWNLOAD_URL}/{OPENSSL_CIPD_LINUX_AMD_PATH.replace("amd64", GetHostSysrootPlatform())}'
+                    f'/+/version:2@{OPENSSL_CIPD_LINUX_AMD_VERSION}')
\x20
     if os.path.exists(ssl_dir):
         RmTree(ssl_dir)
@@ -515,7 +515,7 @@ def RustTargetTriple():
     elif sys.platform == 'win32':
         return 'x86_64-pc-windows-msvc'
     else:
-        return 'x86_64-unknown-linux-gnu'
+        return f'{platform.machine()}-unknown-linux-gnu'
\x20
\x20
 # Build the LLVM libraries and install them .
@@ -526,6 +526,9 @@ def BuildLLVMLibraries(skip_build):
             sys.executable,
             os.path.join(CLANG_SCRIPTS_DIR, 'build.py'),
             '--disable-asserts',
+            '--use-system-cmake',
+            '--host-cc=clang',
+            '--host-cxx=clang++',
             '--no-tools',
             '--no-runtimes',
             # PIC needed for Rust build (links LLVM into shared object)
@@ -678,7 +681,8 @@ def main():
         # Fetch sysroot we build rustc against. This ensures a minimum supported
         # host (not Chromium target). Since the rustc linux package is for
         # x86_64 only, that is the sole needed sysroot.
-        debian_sysroot = DownloadDebianSysroot('amd64', args.skip_checkout)
+        debian_sysroot = DownloadDebianSysroot(
+            GetHostSysrootPlatform(), args.skip_checkout)
\x20
     # Require zlib compression.
     if sys.platform == 'win32':
--- a/tools/rust/cargo-config.toml.template
+++ b/tools/rust/cargo-config.toml.template
@@ -21,3 +21,8 @@ host-config = true
 # Use the same sysroot for host artifacts as target artifacts. Target rustflags
 # are configured via environment variables.
 rustflags = ["-Clink-arg=--sysroot=$DEBIAN_SYSROOT"]
+
+[host.aarch64-unknown-linux-gnu]
+# Use the same sysroot for host artifacts as target artifacts. Target rustflags
+# are configured via environment variables.
+rustflags = ["-Clink-arg=--sysroot=$DEBIAN_SYSROOT"]
--- a/tools/rust/config.toml.template
+++ b/tools/rust/config.toml.template
@@ -87,3 +87,12 @@ cc = "$LLVM_BIN/clang"
 cxx = "$LLVM_BIN/clang++"
 linker = "$LLVM_BIN/clang"
\x20
+[target.aarch64-unknown-linux-gnu]
+llvm-config = "$LLVM_BIN/llvm-config"
+# TODO(danakj): We don't ship this in the clang toolchain package.
+# ranlib = "$LLVM_BIN/llvm-ranlib"
+ar = "$LLVM_BIN/llvm-ar"
+cc = "$LLVM_BIN/clang"
+cxx = "$LLVM_BIN/clang++"
+linker = "$LLVM_BIN/clang"
+
--- a/tools/rust/build_bindgen.py
+++ b/tools/rust/build_bindgen.py
@@ -23,7 +23,7 @@ sys.path.append(
                  'scripts'))
\x20
 from build import (CheckoutGitRepo, DownloadAndUnpack, LLVM_BUILD_TOOLS_DIR,
-                   DownloadDebianSysroot, RunCommand)
+                   DownloadDebianSysroot, GetHostSysrootPlatform, RunCommand)
 from update import (RmTree)
\x20
 # The git hash to use.
@@ -66,7 +66,7 @@ def InstallRustBetaSysroot(rust_git_hash
 def FetchNcurseswLibrary():
     assert sys.platform.startswith('linux')
     ncursesw_dir = os.path.join(LLVM_BUILD_TOOLS_DIR, 'ncursesw')
-    ncursesw_url = (f'{CIPD_DOWNLOAD_URL}/{NCURSESW_CIPD_LINUX_AMD_PATH}'
+    ncursesw_url = (f'{CIPD_DOWNLOAD_URL}/{NCURSESW_CIPD_LINUX_AMD_PATH.replace("amd64", GetHostSysrootPlatform())}'
                     f'/+/version:2@{NCURSESW_CIPD_LINUX_AMD_VERSION}')
\x20
     if os.path.exists(ncursesw_dir):
@@ -146,7 +146,7 @@ def RunCargo(cargo_args):
\x20
     if sys.platform.startswith('linux'):
         # We use these flags to avoid linking with the system libstdc++.
-        sysroot = DownloadDebianSysroot('amd64')
+        sysroot = DownloadDebianSysroot(GetHostSysrootPlatform())
         sysroot_flag = f'--sysroot={sysroot}'
         env['CFLAGS'] += f' {sysroot_flag}'
         env['CXXFLAGS'] += f' {sysroot_flag}'
--- a/tools/clang/scripts/build.py
+++ b/tools/clang/scripts/build.py
@@ -483,6 +483,21 @@ def DownloadPinnedClang():
                            PINNED_CLANG_VERSION)
\x20
\x20
+def GetHostSysrootPlatform():
+  assert sys.platform == 'linux', \\
+    "This patch only applies to Linux, where platform.machine() is predictable"
+
+  arch = platform.machine()
+  return {
+    "aarch64": "arm64",
+    "aarch64_be": "arm64",
+    "armv7l": "arm",
+    "armv8b": "arm64",
+    "armv8l": "arm64",
+    "x86_64": "amd64",
+  }.get(arch, arch)
+
+
 def VerifyVersionOfBuiltClangMatchesVERSION():
   """Checks that `clang --version` outputs RELEASE_VERSION. If this
   fails, update.RELEASE_VERSION is out-of-date and needs to be updated (possibly
'''
CORRECTED_PATCH = PINNED_PATCH.replace("@@ -55,7 +55,7 @@", "@@ -55,8 +55,8 @@", 1)


def source_fixture(patched=False):
    """Build sparse file contents from every hunk, ignoring the broken count."""
    files = {}
    for section in PINNED_PATCH.split("--- a/")[1:]:
        name, _, body = section.partition("\n")
        lines = []
        delta = 0
        hunks = re.split(r"^@@ -(\d+),\d+ \+\d+,\d+ @@[^\n]*\n", body,
                         flags=re.MULTILINE)
        for index in range(1, len(hunks), 2):
            start = int(hunks[index]) - 1
            hunk = hunks[index + 1].splitlines(keepends=True)
            old = [line[1:] for line in hunk if line.startswith((" ", "-"))]
            new = [line[1:] for line in hunk if line.startswith((" ", "+"))]
            old_end = len(lines) - (delta if patched else 0)
            lines.extend(f"# fixture line {i + 1}\n" for i in range(old_end, start))
            lines.extend(new if patched else old)
            delta += len(new) - len(old)
        files[name] = "".join(lines)
    return files


class PortableLinuxPatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="chromix portablelinux patch ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.patches = self.root / "patches"
        self.patch = self.patches / PATCH_REL
        self.patch.parent.mkdir(parents=True)
        self.patch.write_text(PINNED_PATCH)
        script = PREPARE.read_text()
        start = script.index('if [ "$PLATFORM" = linux ]; then\n')
        end = script.index('\nPATCH_BIN=', start)
        self.repair = script[start:end]

    def run_repair(self, platform="linux"):
        env = dict(os.environ, PLATFORM=platform, PLATFORM_PATCHES=str(self.patches))
        env.pop("BASH_ENV", None)
        return subprocess.run(["bash", "-euo", "pipefail", "-c", self.repair],
                              env=env, capture_output=True, text=True, timeout=15)

    def apply_patch(self):
        patch_bin = shutil.which("gpatch") or shutil.which("patch")
        if not patch_bin:
            self.skipTest("GNU patch is required")
        version = subprocess.run([patch_bin, "--version"], capture_output=True,
                                 text=True, timeout=15)
        if "GNU patch" not in version.stdout:
            self.skipTest("GNU patch is required")
        src = self.root / "src"
        for name, content in source_fixture().items():
            path = src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        result = subprocess.run(
            [patch_bin, "-p1", "--ignore-whitespace", "--forward", "--batch",
             "--no-backup-if-mismatch", "-i", str(self.patch), "-d", str(src)],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return src

    def test_malformed_patch_succeeds_but_silently_skips_four_rust_hunks(self):
        src = self.apply_patch()
        rust_name = "tools/rust/build_rust.py"
        expected = source_fixture()[rust_name].replace(
            "GitCherryPick, GitRevert", "GetHostSysrootPlatform, GitRevert", 1)
        rust = (src / rust_name).read_text()
        self.assertEqual(rust, expected)
        self.assertIn("return 'x86_64-unknown-linux-gnu'", rust)
        self.assertIn("DownloadDebianSysroot('amd64', args.skip_checkout)", rust)
        for name, content in source_fixture(patched=True).items():
            if name != rust_name:
                self.assertEqual((src / name).read_text(), content, name)

    def test_repaired_patch_applies_all_hunks_in_all_six_files(self):
        result = self.run_repair()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patch.read_text(), CORRECTED_PATCH)
        src = self.apply_patch()
        expected = source_fixture(patched=True)
        self.assertEqual(len(expected), 6)
        for name, content in expected.items():
            self.assertEqual((src / name).read_text(), content, name)

    def test_already_corrected_patch_is_not_rewritten(self):
        self.patch.write_text(CORRECTED_PATCH)
        before = self.patch.stat().st_mtime_ns
        for _ in range(2):
            result = self.run_repair()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.patch.read_text(), CORRECTED_PATCH)
            self.assertEqual(self.patch.stat().st_mtime_ns, before)

    def test_unexpected_payload_fails_without_modification(self):
        variants = {
            "missing header": PINNED_PATCH.replace("@@ -55,7 +55,7 @@", "@@ -56,7 +56,7 @@"),
            "changed import": PINNED_PATCH.replace("GitCherryPick", "ChangedImport"),
            "changed later hunk": PINNED_PATCH.replace("platform.machine()", "platform.processor()"),
            "changed corrected patch": CORRECTED_PATCH.replace("platform.machine()", "platform.processor()"),
            "duplicate payload": PINNED_PATCH + PINNED_PATCH,
            "empty patch": "",
        }
        for name, content in variants.items():
            with self.subTest(name=name):
                self.patch.write_text(content)
                result = self.run_repair()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unexpected portablelinux ARM64 patch", result.stderr)
                self.assertEqual(self.patch.read_text(), content)

    def test_missing_patch_fails_without_creating_it(self):
        self.patch.unlink()
        result = self.run_repair()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing portablelinux ARM64 patch", result.stderr)
        self.assertFalse(self.patch.exists())

    def test_macos_does_not_read_or_modify_linux_patch(self):
        self.patch.write_text("not a Linux patch")
        result = self.run_repair("macos")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patch.read_text(), "not a Linux patch")
        self.patch.unlink()
        result = self.run_repair("macos")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.patch.exists())

    def test_repair_precedes_application_and_preserves_layer_order_and_hash(self):
        script = PREPARE.read_text()
        steps = [
            'checkout_pinned "https://github.com/ungoogled-software/$PLATFORM_NAME.git"',
            self.repair,
            'python3 "$CORE_REPO/utils/patches.py" apply "$SRC" "$CORE_REPO/patches"',
            'python3 "$CORE_REPO/utils/patches.py" apply "$SRC" "$PLATFORM_PATCHES"',
            'python3 "$CORE_REPO/utils/prune_binaries.py"',
            'cp -R "$REPO/build/windows/lite-tarball-files/." "$SRC/"',
            '"$REPO/build/apply-patches.sh" "$SRC"',
        ]
        positions = [script.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("paths = [repo / 'build/prepare-ungoogled.sh'", script)
        result = subprocess.run(["bash", "-n", str(PREPARE)], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
