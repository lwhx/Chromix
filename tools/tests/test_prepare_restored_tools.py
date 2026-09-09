"""Exercise host-tool setup with command stubs, never downloads or compilations."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import prepare_restored_build as prepare

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "build/posix/prepare-restored-tools.sh"
BASH32 = Path.home() / ".local/bash-3.2-for-ci/bash"


class PrepareRestoredToolsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="restored tools ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.work = self.root / "work"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.work / "src").mkdir(parents=True)
        (self.repo / "build/posix").mkdir(parents=True)
        (self.repo / "tools").mkdir()
        shutil.copy2(SCRIPT, self.repo / "build/posix/prepare-restored-tools.sh")
        (self.repo / "tools/prepare_restored_build.py").write_text('''\
import json, os
from pathlib import Path
def record(name):
    with Path(os.environ["COMMAND_LOG"]).open("a") as stream:
        stream.write(json.dumps([name]) + "\\n")
def verify_tooling(*args): record("verify-pinned-tooling")
def prepare_tooling_links(*args, host_arch=None):
    expected = {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}[os.environ["TEST_MACHINE"]]
    assert host_arch == expected, (host_arch, expected)
    record("host-and-tooling-links")
def restore_tool_endpoints(*args): record("restore-known-endpoints")
def repair_linux_arm64_tool_script(*args): record("repair-linux-arm64-tool-script")
''')
        self.executable("uname", '''\
import os, sys
print(os.environ["TEST_MACHINE"] if sys.argv[1] == "-m" else os.environ["TEST_SYSTEM"])
''')
        self.executable("python3", '''\
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] in ("-c", "-"):
    os.execv(sys.executable, [sys.executable] + args)
with Path(os.environ["COMMAND_LOG"]).open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
if Path(args[0]).name == "prepare_restored_build.py":
    phase = args[args.index("--phase") + 1]
    if phase == "inspect":
        if "TEST_PACKAGE_BINDGEN" in os.environ:
            src = Path(args[args.index("--workdir") + 1]) / "src"
            def native(name):
                path = src / name
                return path.is_file() and path.read_text() == "native"
            inspection = json.loads(os.environ["INSPECT_RESULT"])
            inspection["compilers_native"] = all(native(name) for name in (
                "third_party/rust-toolchain/bin/rustc", "third_party/llvm-build/Release+Asserts/bin/clang"))
            inspection["tools"]["bindgen"]["native"] = native("third_party/rust-toolchain/bin/bindgen")
            inspection["native_tools"] = inspection["compilers_native"] and inspection["tools"]["bindgen"]["native"]
            print(json.dumps(inspection))
        else:
            print(os.environ["INSPECT_RESULT"])
    else:
        print('{"ready_for_gn": true}')
elif Path(args[0]).name in ("update_rust.py", "update.py") and "TEST_PACKAGE_BINDGEN" in os.environ:
    os.execv(sys.executable, [sys.executable] + args)
elif Path(args[0]).name not in ("retrieve_and_unpack_resource.py", "build.py", "build_rust.py", "build_bindgen.py", "update_rust.py", "update.py", "install-sysroot.py"):
    raise SystemExit("unexpected command; downloads/builds are forbidden in this test")
''')

    def executable(self, name, source):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)

    def run_helper(self, platform, arch, *, compatible=False, ci=True, bash=None, machine=None, package_bindgen=None):
        inspection = {"native_tools": compatible, "compilers_native": compatible,
                      "tools": {name: {"native": compatible} for name in ("bindgen", "node")}}
        log = self.root / "commands.jsonl"
        log.unlink(missing_ok=True)
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                   TEST_SYSTEM="Darwin" if platform == "macos" else "Linux",
                   TEST_MACHINE=machine or ("x86_64" if arch == "x64" else "arm64" if platform == "macos" else "aarch64"),
                   GITHUB_ACTIONS="true" if ci else "false", INSPECT_RESULT=json.dumps(inspection), COMMAND_LOG=str(log))
        if package_bindgen is not None:
            env["TEST_PACKAGE_BINDGEN"] = "1" if package_bindgen else "0"
        result = subprocess.run([str(bash or shutil.which("bash")), str(self.repo / "build/posix/prepare-restored-tools.sh"),
                                 str(self.work), platform, arch], capture_output=True, text=True, timeout=30, env=env)
        entries = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, entries

    def test_mac_intel_only_retrieves_platform_resources_and_builds_bindgen(self):
        result, commands = self.run_helper("macos", "x64")
        self.assertEqual(result.returncode, 0, result.stderr)
        retrieval = [command for command in commands if command[0].endswith("retrieve_and_unpack_resource.py")]
        self.assertEqual(retrieval, [[str(self.work / "tooling/ungoogled-chromium-macos/retrieve_and_unpack_resource.py"), "-p", "x86_64"]])
        self.assertIn(["verify-pinned-tooling"], commands)
        self.assertIn(["host-and-tooling-links"], commands)
        self.assertLess(commands.index(["restore-known-endpoints"]), commands.index(retrieval[0]))
        self.assertIn(["tools/rust/build_bindgen.py", "--skip-test"], commands)
        self.assertEqual(commands[-1][1:3], ["--phase", "finish"])
        self.assertTrue((self.work / "src/.chromix-toolchain-ready").exists())
        self.assertFalse(any("-g" in command or "--all" in command or "clone.py" in command for command in commands))

    def test_mac_arm_reuses_native_resources_without_download_or_build(self):
        result, commands = self.run_helper("macos", "arm64", compatible=True, ci=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(commands), 4)
        self.assertEqual(commands[1:3], [["verify-pinned-tooling"], ["host-and-tooling-links"]])
        self.assertEqual(commands[-1][1:3], ["--phase", "finish"])

    def test_linux_arm_runs_original_native_compiler_sequence_and_sysroot(self):
        result, commands = self.run_helper("linux", "arm64")
        self.assertEqual(result.returncode, 0, result.stderr)
        clang = ["tools/clang/scripts/build.py", "--without-fuchsia", "--without-android", "--disable-asserts",
                 "--host-cc=clang", "--host-cxx=clang++", "--use-system-cmake", "--with-ml-inliner-model="]
        rust = ["tools/rust/build_rust.py", "--skip-test"]
        sysroot = ["build/linux/sysroot_scripts/install-sysroot.py", "--arch=arm64"]
        bindgen = ["tools/rust/build_bindgen.py", "--skip-test"]
        for command in (clang, rust, sysroot, bindgen):
            self.assertIn(command, commands)
        self.assertIn(["repair-linux-arm64-tool-script"], commands)
        self.assertLess(commands.index(["repair-linux-arm64-tool-script"]), commands.index(clang))
        self.assertLess(commands.index(clang), commands.index(rust))
        self.assertLess(commands.index(rust), commands.index(sysroot))
        self.assertLess(commands.index(sysroot), commands.index(bindgen))
        self.assertFalse(any("build/build.sh" in command or "ninja" in command for command in commands))

    def x64_package_fixture(self):
        src = self.work / "src"
        for script, root, stamp, binary in (
                ("tools/rust/update_rust.py", "third_party/rust-toolchain", "VERSION", "rustc"),
                ("tools/clang/scripts/update.py", "third_party/llvm-build/Release+Asserts", "cr_build_revision", "clang")):
            toolchain = src / root
            (toolchain / "bin").mkdir(parents=True)
            (toolchain / stamp).write_text("pinned-version")
            (toolchain / "bin" / binary).write_text("wrong-host")
            updater = src / script
            updater.parent.mkdir(parents=True)
            updater.write_text(f'ROOT = {root!r}\nSTAMP = {stamp!r}\nBINARY = {binary!r}\n' + '''\
import os
from pathlib import Path
root = Path.cwd() / ROOT
stamp = root / STAMP
if stamp.is_file() and stamp.read_text() == "pinned-version":
    raise SystemExit(0)
(root / "bin" / BINARY).write_text("native")
if BINARY == "rustc" and os.environ["TEST_PACKAGE_BINDGEN"] == "1":
    (root / "bin/bindgen").write_text("native")
stamp.write_text("pinned-version")
''')
        for name in ("cr_build_revision", "force_head_revision"):
            (src / "third_party/llvm-build" / name).write_text("pinned-version")
        (src / "out/Default/obj").mkdir(parents=True)
        (src / "out/Default/obj/keep.o").write_text("retained object")
        (src / "source.cc").write_text("retained source")

    def test_x64_matching_stamps_force_update_and_reuse_packaged_bindgen(self):
        self.x64_package_fixture()
        result, commands = self.run_helper("linux", "x64", package_bindgen=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for root, binary in (("rust-toolchain", "rustc"), ("llvm-build/Release+Asserts", "clang")):
            self.assertEqual((self.work / "src/third_party" / root / "bin" / binary).read_text(), "native")
        inspections = [command for command in commands if "--phase" in command and "inspect" in command]
        self.assertEqual(len(inspections), 2)
        self.assertLess(commands.index(["tools/clang/scripts/update.py"]), commands.index(inspections[1], 1))
        self.assertNotIn(["tools/rust/build_bindgen.py", "--skip-test"], commands)
        self.assertFalse((self.work / "src/third_party/rust-toolchain-intermediate").exists())
        self.assertFalse((self.work / "src/third_party/llvm-build/force_head_revision").exists())
        self.assertFalse((self.work / "src/third_party/llvm-build/cr_build_revision").exists())
        self.assertEqual((self.work / "src/out/Default/obj/keep.o").read_text(), "retained object")
        self.assertEqual((self.work / "src/source.cc").read_text(), "retained source")
        self.assertEqual(commands[-1][1:3], ["--phase", "finish"])

    def test_x64_package_without_bindgen_fails_without_building_it(self):
        self.x64_package_fixture()
        result, commands = self.run_helper("linux", "x64", package_bindgen=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Rust package lacks executable bindgen", result.stderr)
        self.assertNotIn(["tools/rust/build_bindgen.py", "--skip-test"], commands)
        self.assertFalse(any("finish" in command for command in commands))
        self.assertFalse((self.work / "src/.chromix-toolchain-ready").exists())

    def test_x64_stamp_cleanup_refuses_symlink_without_deleting_other_stamps(self):
        self.x64_package_fixture()
        outside = self.root / "keep"
        outside.write_text("pinned-version")
        stamp = self.work / "src/third_party/llvm-build/Release+Asserts/cr_build_revision"
        stamp.unlink()
        stamp.symlink_to(outside)
        result, commands = self.run_helper("linux", "x64", package_bindgen=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing linked toolchain stamp", result.stderr)
        self.assertEqual(outside.read_text(), "pinned-version")
        self.assertTrue((self.work / "src/third_party/rust-toolchain/VERSION").exists())
        self.assertNotIn(["tools/rust/update_rust.py"], commands)

    def test_x64_local_fallback_preserves_stamps_and_does_not_update(self):
        self.x64_package_fixture()
        result, commands = self.run_helper("linux", "x64", package_bindgen=True, ci=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restricted to GitHub Actions", result.stderr)
        self.assertTrue((self.work / "src/third_party/rust-toolchain/VERSION").exists())
        self.assertNotIn(["tools/rust/update_rust.py"], commands)

    def test_no_local_compilation_or_download(self):
        result, commands = self.run_helper("linux", "arm64", ci=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restricted to GitHub Actions", result.stderr)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][1:3], ["--phase", "inspect"])
        self.assertFalse((self.work / "src/.chromix-toolchain-ready").exists())

    def test_cannot_silently_change_runner(self):
        result, commands = self.run_helper("macos", "x64", machine="arm64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("native macos x64 runner", result.stderr)
        self.assertEqual(commands, [])

    @unittest.skipUnless(BASH32.exists(), "Bash 3.2 is not installed")
    def test_bash32_mac_intel(self):
        result, commands = self.run_helper("macos", "x64", bash=BASH32)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(commands[-1][1:3], ["--phase", "finish"])

    def test_links_are_idempotent_and_restore_only_known_host_paths(self):
        core = self.work / "tooling/ungoogled-chromium"
        platform = self.work / "tooling/ungoogled-chromium-macos"
        core.mkdir(parents=True)
        platform.mkdir()
        (platform / "ungoogled-chromium").mkdir()
        go = self.bin / "go"
        go.write_text("host go")
        with mock.patch.object(prepare.shutil, "which", return_value=str(go)):
            for _ in range(2):
                prepare.prepare_tooling_links(self.work, "macos", "x64")
        self.assertEqual((platform / "build/src").resolve(), self.work / "src")
        self.assertEqual((platform / "build/download_cache").resolve(), self.work / "download_cache")
        self.assertEqual((platform / "ungoogled-chromium").resolve(), core)
        self.assertEqual((self.work / "src/third_party/dawn/tools/golang/mac-amd64/bin/go").resolve(), go)

    def test_linked_parent_is_never_followed_during_host_tool_setup(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.work / "src/third_party").symlink_to(outside)
        with mock.patch.object(prepare.shutil, "which", return_value=str(self.bin / "python3")):
            with self.assertRaisesRegex(ValueError, "linked tooling parent"):
                prepare.prepare_tooling_links(self.work, "linux", "arm64")
        self.assertEqual(list(outside.iterdir()), [])

    def test_restored_linux_arm_completes_pinned_skipped_rust_hunks(self):
        from tools.tests.test_portablelinux_patch import source_fixture
        name = "tools/rust/build_rust.py"
        text = source_fixture()[name].replace("GitCherryPick, GitRevert", "GetHostSysrootPlatform, GitRevert", 1)
        path = self.work / "src" / name
        path.parent.mkdir(parents=True)
        path.write_text(text)
        prepare.repair_linux_arm64_tool_script(self.work / "src")
        updated = path.read_text()
        self.assertIn("return f'{platform.machine()}-unknown-linux-gnu'", updated)
        self.assertIn("'--host-cc=clang'", updated)
        self.assertNotIn("DownloadDebianSysroot('amd64', args.skip_checkout)", updated)
        before = path.stat().st_mtime_ns
        prepare.repair_linux_arm64_tool_script(self.work / "src")
        self.assertEqual(path.stat().st_mtime_ns, before)
        path.write_text("unknown version")
        with self.assertRaisesRegex(ValueError, "unknown restored"):
            prepare.repair_linux_arm64_tool_script(self.work / "src")
        self.assertEqual(path.read_text(), "unknown version")

    def test_only_known_downloader_endpoints_are_restored(self):
        src = self.work / "src"
        names = ("tools/clang/scripts/update.py", "tools/clang/scripts/build.py", "tools/rust/update_rust.py",
                 "tools/rust/build_rust.py", "tools/rust/build_bindgen.py", "build/linux/sysroot_scripts/install-sysroot.py",
                 "build/linux/sysroot_scripts/sysroots.json")
        text = "\n".join(blocked for blocked, endpoint in prepare.ENDPOINTS.values()) + "\nunrelated.qjz9zk"
        for name in names:
            path = src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        source = src / "chrome.cc"
        source.write_text(text)
        self.assertEqual(set(prepare.restore_tool_endpoints(src)), set(names))
        self.assertEqual(prepare.restore_tool_endpoints(src), [])
        self.assertEqual(source.read_text(), text)
        for name in names:
            value = (src / name).read_text()
            self.assertIn("unrelated.qjz9zk", value)
            for blocked, endpoint in prepare.ENDPOINTS.values():
                self.assertNotIn(blocked, value)
                self.assertIn(endpoint, value)


if __name__ == "__main__":
    unittest.main()
