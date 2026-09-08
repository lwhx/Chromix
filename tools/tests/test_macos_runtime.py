"""Tiny Mach-O fixtures and mocked Bash 3.2 builds; no downloads or compilation."""
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import macos_runtime as runtime

REPO = Path(__file__).resolve().parents[2]
BASH32 = Path.home() / ".local/bash-3.2-for-ci/bash"


def binary(path, arch="arm64"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<8I", 0xFEEDFACF, runtime.CPUS[arch], 0, 6, 0, 0, 0, 0))
    return path


def layout(src, arch="arm64"):
    for directory in runtime.LIBRARY_DIRS[:2]:
        binary(src / directory / "libclang.dylib", arch)
    rustc = src / "third_party/rust-toolchain/rustc"
    binary(rustc / "lib/libLLVM.dylib", arch)
    binary(rustc / f"lib/rustlib/{runtime.TRIPLES[arch]}/bin/rust-objcopy", arch).chmod(0o755)
    std = src / f"third_party/rust-toolchain/rust-std-{runtime.TRIPLES[arch]}"
    stdlib = std / f"lib/rustlib/{runtime.TRIPLES[arch]}/lib"
    stdlib.mkdir(parents=True)
    (rustc / f"lib/rustlib/{runtime.TRIPLES[arch]}/lib").symlink_to(stdlib)
    return stdlib / "libLLVM.dylib"


class MacOSRuntimeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mac runtime ' ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.src = self.root / "src"
        self.src.mkdir()

    def test_native_fixed_paths_replace_untrusted_search_paths_without_mutation(self):
        for arch in runtime.TRIPLES:
            with self.subTest(arch=arch):
                src = self.src / arch
                src.mkdir()
                destination = layout(src, arch)
                original = {"PATH": "kept", "DYLD_LIBRARY_PATH": ":/usr/local/lib:/donor/lib:",
                            "DYLD_FALLBACK_LIBRARY_PATH": "/external", "DYLD_FRAMEWORK_PATH": "/frameworks",
                            "DYLD_INSERT_LIBRARIES": "/injected.dylib", "DYLD_VERSIONED_LIBRARY_PATH": "/versions"}
                expected = ":".join(str(src / relative) for relative in runtime.LIBRARY_DIRS)
                with mock.patch.object(subprocess, "run", side_effect=AssertionError("no execution")):
                    env = runtime.runtime_environment(src, arch, original)
                self.assertEqual(env, {"PATH": "kept", "DYLD_LIBRARY_PATH": expected})
                self.assertEqual(original["DYLD_LIBRARY_PATH"], ":/usr/local/lib:/donor/lib:")
                self.assertFalse(destination.exists())
                self.assertEqual(runtime.runtime_environment(src, arch, env), env)

    def test_absent_foreign_or_malformed_libraries_are_not_advertised(self):
        self.assertEqual(runtime.runtime_environment(self.src, "x64", {"DYLD_LIBRARY_PATH": "."}), {})
        directory = self.src / runtime.LIBRARY_DIRS[0]
        library = binary(directory / "libclang.dylib", "arm64")
        self.assertEqual(runtime.runtime_environment(self.src, "x64", {}), {})
        library.write_bytes(b"not a Mach-O")
        self.assertEqual(runtime.runtime_environment(self.src, "arm64", {}), {})
        binary(library)
        library.with_name("libbroken.dylib").symlink_to(directory / "missing")
        self.assertEqual(runtime.runtime_environment(self.src, "arm64", {}), {})

    def test_fat_architectures_are_checked_without_executing(self):
        library = binary(self.src / runtime.LIBRARY_DIRS[0] / "libclang.dylib")
        for magic, width in ((0xCAFEBABE, 20), (0xCAFEBABF, 32)):
            data = struct.pack(">II", magic, 2)
            data += b"".join(struct.pack(">I", cpu) + b"\0" * (width - 4) for cpu in runtime.CPUS.values())
            library.write_bytes(data)
            for arch in runtime.TRIPLES:
                self.assertIn("DYLD_LIBRARY_PATH", runtime.runtime_environment(self.src, arch, {}))
            library.write_bytes(data[:-1])
            self.assertEqual(runtime.runtime_environment(self.src, "arm64", {}), {})
        library.write_bytes(struct.pack(">II", 0xCAFEBABE, 33))
        self.assertEqual(runtime.runtime_environment(self.src, "arm64", {}), {})

    def test_runtime_root_and_architecture_validation(self):
        for name in ("bad:root", "bad\nroot", "bad\rroot"):
            src = self.root / name
            src.mkdir()
            with self.assertRaisesRegex(ValueError, "unsafe"):
                runtime.runtime_environment(src, "arm64", {})
        alias = self.root / "alias"
        alias.symlink_to(self.src)
        with self.assertRaisesRegex(ValueError, "linked"):
            runtime.runtime_environment(alias, "arm64", {})
        with self.assertRaisesRegex(ValueError, "architecture"):
            runtime.runtime_environment(self.src, "../../x64", {})

    def test_directory_and_dylib_symlink_escapes_are_rejected(self):
        outside = self.root / "outside"
        binary(outside / "libclang.dylib")
        directory = self.src / runtime.LIBRARY_DIRS[0]
        directory.parent.mkdir(parents=True)
        directory.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes source"):
            runtime.runtime_environment(self.src, "arm64", {})
        directory.unlink()
        directory.mkdir()
        (directory / "libclang.dylib").symlink_to(outside / "libclang.dylib")
        with self.assertRaisesRegex(ValueError, "escapes source"):
            runtime.runtime_environment(self.src, "arm64", {})

    def test_internal_library_links_are_resolved_and_deduplicated(self):
        directory = self.src / runtime.LIBRARY_DIRS[0]
        binary(directory / "libclang.23.dylib")
        (directory / "libclang.dylib").symlink_to("libclang.23.dylib")
        rustlib = self.src / runtime.LIBRARY_DIRS[1]
        rustlib.parent.mkdir(parents=True)
        rustlib.symlink_to(directory)
        self.assertEqual(runtime.runtime_environment(self.src, "arm64", {}),
                         {"DYLD_LIBRARY_PATH": str(directory)})

    def test_loader_link_is_relative_idempotent_and_preserves_binary_bytes(self):
        for arch in runtime.TRIPLES:
            with self.subTest(arch=arch):
                src = self.src / arch
                src.mkdir()
                destination = layout(src, arch)
                files = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                         for path in src.rglob("*") if path.is_file()}
                runtime.prepare_runtime_loader(src, arch)
                self.assertTrue(destination.is_symlink())
                self.assertFalse(Path(os.readlink(destination)).is_absolute())
                self.assertEqual(destination.resolve(), src / runtime.LIBRARY_DIRS[2] / "libLLVM.dylib")
                before = destination.lstat().st_mtime_ns
                runtime.prepare_runtime_loader(src, arch)
                self.assertEqual(destination.lstat().st_mtime_ns, before)
                self.assertEqual(files, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files})

    def test_missing_or_wrong_architecture_objcopy_fails_without_a_loader_link(self):
        destination = layout(self.src, "arm64")
        with self.assertRaisesRegex(ValueError, "missing or non-native"):
            runtime.prepare_runtime_loader(self.src, "x64")
        self.assertFalse(destination.exists())
        objcopy = self.src / "third_party/rust-toolchain/rustc/lib/rustlib/aarch64-apple-darwin/bin/rust-objcopy"
        binary(objcopy, "x64")
        with self.assertRaisesRegex(ValueError, "missing or non-native"):
            runtime.prepare_runtime_loader(self.src, "arm64")
        self.assertFalse(destination.exists())
        objcopy.unlink()
        with self.assertRaisesRegex(ValueError, "missing or non-native"):
            runtime.prepare_runtime_loader(self.src, "arm64")
        self.assertFalse(destination.exists())

    def test_missing_foreign_llvm_and_missing_directory_fail_without_repair(self):
        destination = layout(self.src)
        library = self.src / runtime.LIBRARY_DIRS[2] / "libLLVM.dylib"
        binary(library, "x64")
        with self.assertRaisesRegex(ValueError, "missing or non-native"):
            runtime.prepare_runtime_loader(self.src, "arm64")
        self.assertFalse(destination.exists())
        library.unlink()
        with self.assertRaisesRegex(ValueError, "missing or non-native"):
            runtime.prepare_runtime_loader(self.src, "arm64")
        self.assertFalse(destination.exists())
        binary(library)
        destination.parent.rmdir()
        with self.assertRaisesRegex(ValueError, "missing nightly Rust runtime library directory"):
            runtime.prepare_runtime_loader(self.src, "arm64")
        self.assertFalse(destination.exists())

    def test_loader_destination_escape_is_rejected_without_writing(self):
        destination = layout(self.src)
        outside = self.root / "external lib"
        outside.mkdir()
        destination.symlink_to(outside / "libLLVM.dylib")
        with self.assertRaisesRegex(ValueError, "escapes source"):
            runtime.prepare_runtime_loader(self.src, "arm64")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(destination.is_symlink())

    def test_loader_keeps_valid_existing_library_and_rejects_foreign_one(self):
        destination = layout(self.src)
        binary(destination)
        before = destination.stat().st_mtime_ns
        runtime.prepare_runtime_loader(self.src, "arm64")
        self.assertFalse(destination.is_symlink())
        self.assertEqual(destination.stat().st_mtime_ns, before)
        binary(destination, "x64")
        with self.assertRaisesRegex(ValueError, "invalid existing"):
            runtime.prepare_runtime_loader(self.src, "arm64")

    def test_source_layout_and_bindgen_wrapper_survive_a_stripped_environment(self):
        upstream = REPO / ".chromix-build-mac-verify/src/build/rust/gni_impl/run_bindgen.py"
        if not upstream.is_file():
            self.skipTest("local pinned Chromium wrapper required")
        import importlib.util
        import types
        action_helpers = types.ModuleType("action_helpers")
        filter_args = types.ModuleType("filter_clang_args")
        filter_args.filter_clang_args = lambda values: values
        spec = importlib.util.spec_from_file_location("pinned_run_bindgen", upstream)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, action_helpers=action_helpers, filter_clang_args=filter_args), \
                mock.patch.object(sys, "path", list(sys.path)):
            spec.loader.exec_module(module)
        destination = layout(self.src)
        runtime.prepare_runtime_loader(self.src, "arm64")
        library = self.src / runtime.LIBRARY_DIRS[0]
        output = self.root / "bindings.rs"
        output.write_text("// mocked bindings\n")
        calls = []
        def check_call(command, **kwargs):
            calls.append((command, dict(kwargs.get("env", os.environ))))
        argv = [str(upstream), "--bindgen-exe", "bindgen", "--rustfmt-exe", "rustfmt",
                "--header", "input.h", "--output", str(output),
                "--ld-library-path", str(library), "--libclang-path", str(library)]
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(module.subprocess, "check_call", side_effect=check_call):
            module.main()
        self.assertEqual(calls[0][0][0], "bindgen")
        self.assertEqual(calls[0][1]["DYLD_LIBRARY_PATH"], str(library))
        self.assertEqual(calls[0][1]["LIBCLANG_PATH"], str(library))
        objcopy = self.src / "third_party/rust-toolchain/rustc/lib/rustlib/aarch64-apple-darwin/bin/rust-objcopy"
        self.assertEqual((objcopy.parent / "../lib/libLLVM.dylib").resolve(), destination.resolve())
        self.assertTrue(destination.is_file())

    def test_read_only_probe_environment_and_loader_fix_preserve_compiler_objects(self):
        from tools import prepare_restored_build as prepare
        from tools.tests.test_prepare_restored_build import PrepareRestoredBuildTest
        fixture = PrepareRestoredBuildTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture()
        layout(fixture.src)
        output = fixture.object("keep.o")
        fixture.deps({"obj/keep.o": ["../../include/a.h"]})
        before = output.stat().st_mtime_ns
        probe_env = runtime.runtime_environment(fixture.src, "arm64", {})
        def run(command, **kwargs):
            env = kwargs.get("env", os.environ)
            native = Path(command[0]).name != "bindgen" or env.get("DYLD_LIBRARY_PATH") == probe_env["DYLD_LIBRARY_PATH"]
            return mock.Mock(returncode=0 if native else -6, stdout="native tool" if native else "dyld: no LC_RPATH")
        with fixture.native_context(), mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(prepare.subprocess, "run", side_effect=run):
            initial = prepare.prepare(fixture.work, "macos", "arm64", phase="inspect")
            runtime.prepare_runtime_loader(fixture.src, "arm64")
            finished = prepare.prepare(fixture.work, "macos", "arm64")
            resumed = prepare.prepare(fixture.work, "macos", "arm64")
        self.assertFalse(initial["needs_invalidation"])
        self.assertEqual(output.stat().st_mtime_ns, before)
        for result in (finished, resumed):
            self.assertEqual(result["counters"]["tool_swap_invalidations"], 0)
            self.assertEqual(result["counters"]["toolchain_invalidated_outputs"], 0)

    def test_native_post_link_probe_for_both_architectures_has_no_dyld_overrides(self):
        for arch in runtime.TRIPLES:
            with self.subTest(arch=arch):
                src = self.src / arch
                src.mkdir()
                layout(src, arch)
                runtime.prepare_runtime_loader(src, arch)
                objcopy = src / f"third_party/rust-toolchain/rustc/lib/rustlib/{runtime.TRIPLES[arch]}/bin/rust-objcopy"
                inherited = {"PATH": "/kept", "DYLD_LIBRARY_PATH": "/bad", "DYLD_INSERT_LIBRARIES": "/bad",
                             "DYLD_FALLBACK_LIBRARY_PATH": "/bad", "DYLD_UNKNOWN_OVERRIDE": "/bad"}
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(runtime.platform, "system", return_value="Darwin"), \
                        mock.patch.object(runtime.platform, "machine", return_value="arm64" if arch == "arm64" else "x86_64"), \
                        mock.patch.dict(os.environ, inherited, clear=True), \
                        mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(
                            returncode=0, stdout="LLVM version 23\n" + "x" * 4000)) as run, \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    status = runtime.main(["--src", str(src), "--arch", arch, "--verify-loader"])
                self.assertEqual(status, 0, stderr.getvalue())
                run.assert_called_once_with([str(objcopy), "--version"], cwd=src, env={"PATH": "/kept"},
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, errors="replace", timeout=10, check=False)
                self.assertIn("LLVM version 23", stderr.getvalue())
                self.assertLess(len(stderr.getvalue()), 2200)
                self.assertIn("export DYLD_LIBRARY_PATH=", stdout.getvalue())
                self.assertIn("unset -- DYLD_FALLBACK_LIBRARY_PATH", stdout.getvalue())
                self.assertNotIn("LLVM version", stdout.getvalue())

    def test_probe_failure_and_timeout_fail_cli_with_bounded_stderr_only(self):
        layout(self.src)
        runtime.prepare_runtime_loader(self.src, "arm64")
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                stdout, stderr = io.StringIO(), io.StringIO()
                kwargs = {"side_effect": subprocess.TimeoutExpired("rust-objcopy", 10, output=b"dyld: " + b"x" * 5000)} if timeout else {
                    "return_value": mock.Mock(returncode=-6, stdout="dyld: " + "x" * 5000)}
                with mock.patch.object(runtime.platform, "system", return_value="Darwin"), \
                        mock.patch.object(runtime.platform, "machine", return_value="arm64"), \
                        mock.patch.object(runtime.subprocess, "run", **kwargs) as run, \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    status = runtime.main(["--src", str(self.src), "--arch", "arm64", "--verify-loader"])
                self.assertEqual(status, 1)
                run.assert_called_once()
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("dyld:", stderr.getvalue())
                self.assertIn("timed out after 10 seconds" if timeout else "exited -6", stderr.getvalue())
                self.assertLess(len(stderr.getvalue()), 2400)

    def test_probe_requires_native_host_before_execution(self):
        layout(self.src)
        runtime.prepare_runtime_loader(self.src, "arm64")
        for system, machine in (("Linux", "arm64"), ("Darwin", "x86_64"), ("Darwin", "unknown")):
            with self.subTest(system=system, machine=machine), \
                    mock.patch.object(runtime.platform, "system", return_value=system), \
                    mock.patch.object(runtime.platform, "machine", return_value=machine), \
                    mock.patch.object(runtime.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "native macOS arm64 host"):
                    runtime.verify_runtime_loader(self.src, "arm64")
                run.assert_not_called()

    def test_probe_rejects_missing_link_foreign_headers_and_unexecutable_objcopy(self):
        destination = layout(self.src)
        objcopy = self.src / "third_party/rust-toolchain/rustc/lib/rustlib/aarch64-apple-darwin/bin/rust-objcopy"
        library = self.src / runtime.LIBRARY_DIRS[2] / "libLLVM.dylib"
        with mock.patch.object(runtime.platform, "system", return_value="Darwin"), \
                mock.patch.object(runtime.platform, "machine", return_value="arm64"), \
                mock.patch.object(runtime.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "repaired Rust LLVM runtime"):
                runtime.verify_runtime_loader(self.src, "arm64")
            self.assertFalse(destination.exists())
            runtime.prepare_runtime_loader(self.src, "arm64")
            for path in (objcopy, library):
                binary(path, "x64")
                with self.assertRaisesRegex(ValueError, "missing or non-native"):
                    runtime.verify_runtime_loader(self.src, "arm64")
                binary(path)
            objcopy.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "not executable"):
                runtime.verify_runtime_loader(self.src, "arm64")
            run.assert_not_called()

    def test_read_only_cli_and_prepare_loader_never_probe(self):
        destination = layout(self.src)
        for flags in ([], ["--prepare-loader"]):
            stdout = io.StringIO()
            with mock.patch.object(runtime.subprocess, "run") as run, redirect_stdout(stdout):
                status = runtime.main(["--src", str(self.src), "--arch", "arm64", *flags])
            self.assertEqual(status, 0)
            run.assert_not_called()
            self.assertEqual(destination.exists(), bool(flags))

    def test_cli_failure_emits_no_shell_code(self):
        result = subprocess.run([sys.executable, str(REPO / "tools/macos_runtime.py"),
                                 "--src", str(self.src / "absent"), "--arch", "arm64"],
                                text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


MOCK_COMMAND = r'''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "uname":
    print(os.environ["MACHINE"] if args == ["-m"] else "Darwin")
    raise SystemExit(0)
if name == "python3" and args[0] == "-c":
    os.execv(sys.executable, [sys.executable] + args)
if name == "python3" and Path(args[0]).name == "macos_runtime.py":
    if "--verify-loader" in args:
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("mocked_runtime", args[0])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        def probe(command, **kwargs):
            assert command[1:] == ["--version"] and Path(command[0]).name == "rust-objcopy"
            assert kwargs["timeout"] == 10 and not any(key.startswith("DYLD_") for key in kwargs["env"])
            assert Path(os.environ["LOADER_LINK"]).is_file()
            with Path(os.environ["CALL_LOG"]).open("a") as stream:
                stream.write(json.dumps(["verify-loader", args, os.environ.get("DYLD_LIBRARY_PATH")]) + "\n")
            return mock.Mock(returncode=-6 if os.environ.get("FAIL_LOADER_PROBE") else 0, stdout="mocked LLVM version")
        with mock.patch.object(module.platform, "system", return_value="Darwin"), \
                mock.patch.object(module.platform, "machine", return_value=os.environ["MACHINE"]), \
                mock.patch.object(module.subprocess, "run", side_effect=probe):
            raise SystemExit(module.main(args[1:]))
    os.execv(sys.executable, [sys.executable] + args)
if name == "python3":
    name = Path(args[0]).name
with Path(os.environ["CALL_LOG"]).open("a") as stream:
    stream.write(json.dumps([name, args, os.environ.get("DYLD_LIBRARY_PATH")]) + "\n")
if name == "prepare_restored_build.py":
    phase = args[args.index("--phase") + 1]
    expected = os.environ["EXPECTED_RUNTIME"]
    native = os.environ.get("DYLD_LIBRARY_PATH") == expected
    if phase == "inspect":
        print(json.dumps({"compilers_native": os.environ.get("RETRIEVE_RUNTIME") != "1", "tools": {
            "bindgen": {"native": native and os.environ.get("REBUILD_BINDGEN") != "1"},
            "node": {"native": True}}}))
    else:
        if not native:
            raise SystemExit("finish lost runtime environment")
        destination = Path(os.environ["LOADER_LINK"])
        if not destination.is_file():
            raise SystemExit("finish lacks the Rust LLVM loader link")
        print('{"ready_for_gn": true}')
elif name == "retrieve_and_unpack_resource.py":
    if os.environ.get("RETRIEVE_RUNTIME") != "1":
        raise SystemExit("unexpected resource retrieval")
    import struct
    library = Path(os.environ["LLVM_RUNTIME"])
    library.write_bytes(struct.pack("<8I", 0xFEEDFACF, 0x1000007, 0, 6, 0, 0, 0, 0))
elif name == "build_bindgen.py":
    if os.environ.get("REBUILD_BINDGEN") != "1":
        raise SystemExit("unexpected bindgen rebuild")
    if os.environ.get("DYLD_LIBRARY_PATH") != os.environ["EXPECTED_RUNTIME"]:
        raise SystemExit("bindgen build lost runtime environment")
    if not Path(os.environ["LOADER_LINK"]).is_file():
        raise SystemExit("bindgen build lacks loader link")
elif name == "-":
    pass
elif name not in ("prepare-ungoogled.sh", "gn", "ninja", "merge_gn_args.py"):
    raise SystemExit("unexpected command; no downloads/builds: " + name)
'''


class MacOSRuntimeShellTest(unittest.TestCase):
    SHELL = shutil.which("bash")

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mac shell ' ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.work = self.root / "work"
        self.src = self.work / "src"
        self.src.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        for relative in ("build/macos/build.sh", "build/posix/prepare-restored-tools.sh", "tools/macos_runtime.py"):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / relative, target)
        self.executable(self.bin / "uname")
        (self.bin / "bash").symlink_to(self.SHELL)
        self.executable(self.bin / "python3")
        self.executable(self.bin / "ninja")
        self.executable(self.repo / "build/prepare-ungoogled.sh")
        (self.repo / "build/macos/select-xcode.sh").write_text("select_macos_xcode() { :; }\n")
        (self.repo / "build/posix/upstream-cache.sh").write_text(
            'chromix_select_restored_ninja() { CHROMIX_NINJA=ninja; }\n'
            'chromix_import_upstream_cache() { return 99; }\n'
            'chromix_build_restored_target() { shift 2; "$CHROMIX_NINJA" -C "$OUT" "$@"; }\n'
            'chromix_report_upstream_plan() { "$CHROMIX_NINJA" -C "$OUT" -n "$@"; }\n')
        self.executable(self.src / "out/Default/gn")
        (self.src / ".chromix-upstream-restored.json").write_text("{}")
        self.object = self.src / "out/Default/obj/keep.o"
        self.object.parent.mkdir()
        self.object.write_text("retained compiler object")
        self.before = self.object.stat().st_mtime_ns
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("DYLD_") and key not in ("BASH_ENV", "ENV")}
        self.env.update(PATH=str(self.bin) + os.pathsep + os.environ["PATH"], CALL_LOG=str(self.log),
                        CHROMIX_APPLY_DOMAIN_SUBSTITUTION="0", CHROMIX_JOBS="1", GITHUB_ACTIONS="false")

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!{sys.executable}\n" + MOCK_COMMAND)
        path.chmod(0o755)

    def configure(self, arch):
        destination = layout(self.src, arch)
        expected = ":".join(str(self.src / relative) for relative in runtime.LIBRARY_DIRS)
        self.env.update(MACHINE="arm64" if arch == "arm64" else "x86_64", EXPECTED_RUNTIME=expected,
                        LOADER_LINK=str(destination), LLVM_RUNTIME=str(self.src / runtime.LIBRARY_DIRS[2] / "libLLVM.dylib"))
        return expected

    def run_script(self, relative, *args):
        return subprocess.run([str(self.SHELL), "-euo", "pipefail", str(self.repo / relative), *args],
                              env=self.env, text=True, capture_output=True, timeout=20)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_exports_before_inspect_and_finish_without_rebuilding_bindgen(self):
        expected = self.configure("arm64")
        result = self.run_script("build/posix/prepare-restored-tools.sh", str(self.work), "macos", "arm64")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual(calls[0][1][1:3], ["--phase", "inspect"])
        self.assertEqual(calls[-1][1][1:3], ["--phase", "finish"])
        self.assertTrue(all(env == expected for _, _, env in calls))
        self.assertFalse(any(name == "build_bindgen.py" for name, _, _ in calls))
        self.assertEqual(self.object.stat().st_mtime_ns, self.before)

    def test_actual_bindgen_build_and_finish_receive_environment(self):
        expected = self.configure("x64")
        self.env.update(REBUILD_BINDGEN="1", GITHUB_ACTIONS="true")
        result = self.run_script("build/posix/prepare-restored-tools.sh", str(self.work), "macos", "x64")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("build_bindgen.py", [name for name, _, _ in self.calls()])
        self.assertTrue(all(env == expected for _, _, env in self.calls()))

    def test_builder_parent_child_gn_and_ninja_keep_runtime_for_both_arches(self):
        for arch in runtime.TRIPLES:
            with self.subTest(arch=arch):
                if arch != "arm64":
                    shutil.rmtree(self.src / "third_party")
                    self.log.unlink()
                expected = self.configure(arch)
                result = self.run_script("build/macos/build.sh", str(self.work), arch)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                calls = self.calls()
                self.assertEqual(calls[0][0], "prepare-ungoogled.sh")
                self.assertIn("prepare_restored_build.py", [name for name, _, _ in calls])
                self.assertEqual([name for name, _, _ in calls[-3:]], ["gn", "ninja", "ninja"])
                self.assertTrue(all(env == expected for _, _, env in calls))
                self.assertEqual(self.object.stat().st_mtime_ns, self.before)

    def test_retrieval_refreshes_child_and_parent_runtime_paths(self):
        expected = self.configure("x64")
        (self.src / runtime.LIBRARY_DIRS[2] / "libLLVM.dylib").unlink()
        self.env.update(RETRIEVE_RUNTIME="1", REBUILD_BINDGEN="1", GITHUB_ACTIONS="true")
        result = self.run_script("build/macos/build.sh", str(self.work), "x64")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertNotEqual(calls[0][2], expected)
        self.assertIn("retrieve_and_unpack_resource.py", [name for name, _, _ in calls])
        rebuild = next(index for index, (name, _, _) in enumerate(calls) if name == "build_bindgen.py")
        self.assertTrue(all(env == expected for _, _, env in calls[rebuild:]))
        self.assertEqual([name for name, _, _ in calls[-3:]], ["gn", "ninja", "ninja"])

    def test_failed_loader_probe_blocks_bindgen_finish_and_ready_marker(self):
        self.configure("arm64")
        self.env.update(FAIL_LOADER_PROBE="1", REBUILD_BINDGEN="1", GITHUB_ACTIONS="true")
        result = self.run_script("build/posix/prepare-restored-tools.sh", str(self.work), "macos", "arm64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Rust LLVM loader probe exited -6", result.stderr)
        calls = self.calls()
        self.assertEqual(calls[-1][0], "verify-loader")
        self.assertFalse(any(name == "build_bindgen.py" or "finish" in args for name, args, _ in calls))
        self.assertFalse((self.src / ".chromix-toolchain-ready").exists())
        self.assertEqual(self.object.stat().st_mtime_ns, self.before)

    def test_cli_scrubs_all_inherited_dyld_overrides_in_bash(self):
        self.configure("arm64")
        env = dict(self.env, DYLD_LIBRARY_PATH="/bad", DYLD_INSERT_LIBRARIES="/bad",
                   DYLD_FALLBACK_LIBRARY_PATH="/bad", DYLD_VERSIONED_FRAMEWORK_PATH="/bad")
        command = 'value="$("$1" "$2" --src "$3" --arch arm64)"; eval "$value"; "$1" -c '\
                  "'import json, os; print(json.dumps({k: v for k, v in os.environ.items() if k.startswith(\"DYLD_\")}))'"
        result = subprocess.run([str(self.SHELL), "-euo", "pipefail", "-c", command, "fixture",
                                 sys.executable, str(REPO / "tools/macos_runtime.py"), str(self.src)],
                                env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"DYLD_LIBRARY_PATH": self.env["EXPECTED_RUNTIME"]})

    def test_wrong_host_fails_before_any_probe_or_loader_write(self):
        self.configure("arm64")
        for relative, args in (("build/macos/build.sh", (str(self.work), "x64")),
                               ("build/posix/prepare-restored-tools.sh", (str(self.work), "macos", "x64"))):
            result = self.run_script(relative, *args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("native", result.stderr)
            self.assertFalse(self.log.exists())
            self.assertFalse(Path(self.env["LOADER_LINK"]).exists())

    def test_quoted_shell_output_does_not_execute_path_contents(self):
        src = self.root / "$(touch INJECTED) ' runtime"
        src.mkdir()
        layout(src)
        command = 'value="$("$1" "$2" --src "$3" --arch arm64)"; eval "$value"; "$1" -c '\
                  "'import os; print(os.environ[\"DYLD_LIBRARY_PATH\"])'"
        result = subprocess.run([str(self.SHELL), "-euo", "pipefail", "-c", command, "fixture",
                                 sys.executable, str(REPO / "tools/macos_runtime.py"), str(src)],
                                cwd=self.root, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), ":".join(str(src / p) for p in runtime.LIBRARY_DIRS))
        self.assertFalse((self.root / "INJECTED").exists())


@unittest.skipUnless(BASH32.is_file(), "locally built Bash 3.2 required")
class MacOSRuntimeBash32Test(MacOSRuntimeShellTest):
    SHELL = BASH32


if __name__ == "__main__":
    unittest.main()
