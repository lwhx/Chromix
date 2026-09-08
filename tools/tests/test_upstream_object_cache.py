import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import upstream_object_cache as cache


CLANG = next((Path(name).resolve() for name in (
    shutil.which("clang") or "", "/usr/local/swift/usr/bin/clang", "/usr/bin/clang"
) if name and Path(name).is_file()), None)
NINJA = shutil.which("ninja")
WRAPPER = Path(cache.__file__).resolve()


class NinjaMetadataTest(unittest.TestCase):
    def test_murmur_matches_real_ninja_hash_and_v5_parser(self):
        command = b"cp ../../generated.txt gen/generated.h"
        self.assertEqual(cache.murmur_hash64a(command), 0x7FBDA484803AEEC8)
        self.assertNotEqual(cache.murmur_hash64a(command, 0xDECAFBAD), 0x7FBDA484803AEEC8)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".ninja_log"
            path.write_text("# ninja log v5\n0\t2\t10\tgen/generated.h\t7fbda484803aeec8\n")
            self.assertEqual(cache.ninja_log(path)["gen/generated.h"],
                             (10, 0x7FBDA484803AEEC8, 5))

    def test_internal_directory_aliases_inventory_and_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tree"
            (root / "lib/real").mkdir(parents=True)
            (root / "lib/real/runtime.a").write_bytes(b"archive")
            (root / "lib/alias").symlink_to("real")
            (root / "lib/chain").symlink_to("alias")
            (root / "lib/real/back").symlink_to("..")
            entries = cache.tree_inventory(root)
            self.assertEqual(entries["lib/alias"], {"link": "real", "kind": "directory"})
            self.assertNotIn("lib/alias/runtime.a", entries)
            self.assertNotIn("lib/real/back/real", entries)
            cache.verify_tree(root, entries)
            (root / "lib/alias").unlink()
            (root / "lib/alias").symlink_to("../lib/real")
            with self.assertRaisesRegex(cache.Miss, "symlink changed"):
                cache.verify_tree(root, entries)
            (root / "lib/alias").unlink()
            (root / "lib/alias").symlink_to("real")
            (root / "lib/real/runtime.a").write_bytes(b"changed")
            with self.assertRaisesRegex(cache.Miss, "content changed"):
                cache.verify_tree(root, entries)
            for target in ("missing", "alias", str(root / "lib/real"), "../../outside"):
                with self.subTest(target=target):
                    (root / "lib/alias").unlink()
                    (root / "lib/alias").symlink_to(target)
                    with self.assertRaises(cache.Miss):
                        cache.tree_inventory(root)
                    with self.assertRaises(cache.Miss):
                        cache.verify_tree(root, entries)

    def test_object_time_floor_is_opt_in_and_metadata_is_exact(self):
        second = cache.NANOSECOND
        logged, dependency = 10 * second + 123, 11 * second + 456
        with self.assertRaises(cache.Miss):
            cache.object_times(11 * second, dependency, logged, 6, False)
        record = cache.object_times(11 * second, dependency, logged, 6, True)
        self.assertEqual(record["object_mtime"], 11 * second)
        self.assertEqual(record["dependency_mtime"], dependency)
        self.assertEqual(record["freshness_cutoff"], 10 * second)
        self.assertTrue(cache.input_is_fresh(10 * second - 1, record))
        self.assertFalse(cache.input_is_fresh(10 * second, record))
        for actual in (10 * second, 11 * second + 1, dependency + 1):
            with self.subTest(actual=actual), self.assertRaises(cache.Miss):
                cache.object_times(actual, dependency, logged, 6, True)
        with self.assertRaises(cache.Miss):
            cache.object_times(11 * second, dependency, dependency - 1, 5, True)
        with self.assertRaises(cache.Miss):
            cache.object_times(11 * second, dependency, dependency + 1, 6, True)
        v5 = cache.object_times(11 * second, dependency, dependency, 5, True)
        self.assertEqual(v5["freshness_cutoff"], 11 * second)
        exact = cache.object_times(dependency, dependency, dependency, 5, False)
        self.assertTrue(cache.input_is_fresh(dependency, exact))

    def test_only_known_wrapper_is_stripped(self):
        argv = ["../../third_party/llvm-build/Release+Asserts/bin/clang", "-c", "../../a.c"]
        prefix = ["python3", str(WRAPPER), "compile", "--"]
        self.assertEqual(cache.split_command(shlex.join(prefix + argv)), argv)
        self.assertEqual(cache.split_command("ccache " + shlex.join(argv)), ["ccache", *argv])


@unittest.skipUnless(sys.platform.startswith("linux") and CLANG and NINJA,
                     "real Linux Clang and Ninja required")
class UpstreamObjectCacheTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name) / "work"
        self.donor = self.work / "upstream-cache/src"
        self.src = self.work / "src"
        self.old = self.donor / "out/Default"
        self.new = self.src / "out/Chromix"
        self.compiler = f"../../{cache.CLANG}/bin/clang"
        self.flags = ["-nostdinc", "-ffile-compilation-dir=.", "-Werror=date-time",
                      "-O0", "-DNUMBER=2", "-I../../shadow", "-I../../include", "-Igen"]
        self.environment = mock.patch.dict(os.environ, {
            name: value for name, value in os.environ.items()
            if name not in cache.ENVIRONMENT_INPUTS
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.write(self.donor / "a.c", '#include "h.h"\n#include "generated.h"\n'
                   '#if __has_include("optional.h")\n#include "optional.h"\n'
                   '#else\n#define OPTIONAL 0\n#endif\n'
                   'int value(void) { return VALUE + NUMBER + GENERATED + OPTIONAL; }\n')
        self.write(self.donor / "include/h.h", "#define VALUE 17\n")
        self.write(self.donor / "generated.txt", "#define GENERATED 3\n")
        (self.donor / "shadow").mkdir()
        clang = self.donor / cache.CLANG / "bin/clang"
        clang.parent.mkdir(parents=True)
        shutil.copy2(CLANG, clang)
        (clang.parent / "clang++").symlink_to("clang")
        self.write(self.donor / cache.CLANG / "lib/clang/fixture/include/test.h", "/* resource */\n")
        shutil.copytree(self.donor, self.src, symlinks=True)
        self.ninja_file(self.old)
        self.ninja_file(self.new, wrapped=True)
        self.run_ninja(self.old)
        self.donor_object = (self.old / "obj/a.o").read_bytes()
        self.argv = shlex.split(cache.compdb(self.old)[0]["command"])

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)

    def ninja_file(self, directory, wrapped=False, extra="", dependency_flag="-MD"):
        prefix = f"{shlex.quote(sys.executable)} {shlex.quote(str(WRAPPER))} compile -- " if wrapped else ""
        self.write(directory / "build.ninja",
                   "rule gen\n  command = cp ../../generated.txt gen/generated.h\n"
                   "rule cc\n  command = " + prefix + self.compiler + " " + dependency_flag +
                   " -MF $out.d " + shlex.join(self.flags) + " " + extra + " -c $in -o $out\n"
                   "  depfile = $out.d\n  deps = gcc\n"
                   "build gen/generated.h: gen ../../generated.txt\n"
                   "build obj/a.o: cc ../../a.c || gen/generated.h\n"
                   "default obj/a.o\n")

    def run_ninja(self, directory, *args):
        result = subprocess.run([NINJA, "-v", *args], cwd=directory, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def prepare(self, **kwargs):
        return cache.prepare(self.src, self.donor, "linux", "x64", self.work, **kwargs)

    def ready(self, **kwargs):
        report = self.prepare(**kwargs)
        self.assertEqual(report["status"], "ready", report)
        self.assertEqual(report["counts"]["prepared"], 1, report)
        self.assertEqual(report["counts"]["bytes_staged"], len(self.donor_object))
        return report

    def receipt(self):
        return json.loads((self.work / cache.CACHE / "receipts" /
                           f"{cache.key('obj/a.o')}.json").read_text())

    def generate(self):
        self.run_ninja(self.new, "gen/generated.h")
        (self.new / "obj").mkdir(exist_ok=True)

    def direct_compile(self, argv=None, expect_hit=False):
        original = subprocess.call
        cwd = Path.cwd()
        try:
            os.chdir(self.new)
            with mock.patch.object(cache.subprocess, "call", wraps=original) as call:
                result = cache.compile_command(argv or self.argv)
            if expect_hit:
                call.assert_not_called()
            else:
                call.assert_called_once_with(argv or self.argv)
            return result
        finally:
            os.chdir(cwd)

    def assert_miss(self, report, reason):
        self.assertEqual(report["status"], "miss", report)
        self.assertIn(reason, " ".join(report["reasons"]), report)

    def test_real_hit_and_ninja_second_run_no_work(self):
        self.ready()
        self.assertFalse((self.new / "gen/generated.h").exists())
        self.run_ninja(self.new)
        self.assertEqual(self.receipt()["status"], "hit", self.receipt())
        self.assertEqual(self.receipt()["bytes"], len(self.donor_object))
        self.assertEqual((self.new / "obj/a.o").read_bytes(), self.donor_object)
        deps = cache.ninja_deps(self.new / ".ninja_deps")["obj/a.o"][1]
        self.assertIn("gen/generated.h", deps)
        self.assertIn("../../include/h.h", deps)
        self.assertFalse((self.new / "obj/a.o.d").exists())
        self.assertIn("no work to do", self.run_ninja(self.new))
        self.assertEqual(self.direct_compile(expect_hit=True), 0)
        self.assertTrue((self.new / "obj/a.o.d").is_file())

    def test_changed_source_header_and_generated_header_execute_compiler(self):
        self.ready()
        self.generate()
        for relative, replacement in (
            ("a.c", "int value(void) { return 456; }\n"),
            ("include/h.h", "#define VALUE 89\n"),
            ("out/Chromix/gen/generated.h", "#define GENERATED 345\n"),
        ):
            with self.subTest(relative=relative):
                path = self.src / relative
                original = path.read_bytes()
                self.write(path, replacement)
                self.assertEqual(self.direct_compile(), 0)
                self.assertEqual(self.receipt()["status"], "miss")
                self.assertIn("canonical source/header/config changed", self.receipt()["reason"])
                self.assertNotEqual((self.new / "obj/a.o").read_bytes(), self.donor_object)
                path.write_bytes(original)

    def test_changed_flags_and_reordered_flags_execute_original_argv(self):
        self.ready()
        self.generate()
        argv = self.argv.copy()
        argv[argv.index("-DNUMBER=2")] = "-DNUMBER=44"
        self.assertEqual(self.direct_compile(argv), 0)
        self.assertIn("argv differs", self.receipt()["reason"])
        self.assertNotEqual((self.new / "obj/a.o").read_bytes(), self.donor_object)
        argv = self.argv.copy()
        first, second = argv.index("-nostdinc"), argv.index("-O0")
        argv[first], argv[second] = argv[second], argv[first]
        self.assertEqual(self.direct_compile(argv), 0)
        self.assertIn("argv differs", self.receipt()["reason"])

    def test_header_shadow_and_negative_lookup_changes_miss(self):
        self.ready()
        self.generate()
        for name, body in (("h.h", "#define VALUE 789\n"),
                           ("optional.h", "#define OPTIONAL 99\n")):
            with self.subTest(name=name):
                path = self.src / "shadow" / name
                self.write(path, body)
                self.assertEqual(self.direct_compile(), 0)
                self.assertIn("preprocessor", self.receipt()["reason"])
                self.assertNotEqual((self.new / "obj/a.o").read_bytes(), self.donor_object)
                path.unlink()

    def test_negative_lookup_without_include_changes_preprocessor_bytes(self):
        source = '#if __has_include("optional.h")\n#define X 33\n#else\n#define X 11\n#endif\nint value(void){return X;}\n'
        self.write(self.donor / "a.c", source)
        self.write(self.src / "a.c", source)
        self.run_ninja(self.old)
        self.donor_object = (self.old / "obj/a.o").read_bytes()
        self.ready()
        self.generate()
        self.write(self.src / "shadow/optional.h", "/* only existence matters */\n")
        self.assertEqual(self.direct_compile(), 0)
        self.assertIn("preprocessor bytes mismatch", self.receipt()["reason"])

    def test_stale_command_log_latest_record_wins(self):
        path = self.old / ".ninja_log"
        original = path.read_text()
        record = original.splitlines()[-1].split("\t")
        record[-1] = "0"
        path.write_text(original + "\t".join(record) + "\n")
        self.assert_miss(self.prepare(), "stale Ninja command hash")
        self.generate()
        self.assertEqual(self.direct_compile(), 0)
        self.assertEqual(self.receipt()["status"], "miss")

    def test_stale_object_or_dependency_mtime_misses(self):
        obj = self.old / "obj/a.o"
        original = obj.stat().st_mtime_ns
        os.utime(obj, ns=(original + 1000000000, original + 1000000000))
        self.assert_miss(self.prepare(), "stale Ninja object/dependency mtime")
        os.utime(obj, ns=(original, original))
        path = self.old / ".ninja_deps"
        content = bytearray(path.read_bytes())
        offset = 16
        while offset < len(content):
            size, = struct.unpack_from("<I", content, offset)
            if size & 0x80000000:
                struct.pack_into("<Q", content, offset + 8, original - 1)
            offset += 4 + (size & 0x7fffffff)
        path.write_bytes(content)
        self.assert_miss(self.prepare(), "stale Ninja object/dependency mtime")

    def test_stale_dependency_before_and_after_prepare(self):
        self.ready()
        header = self.donor / "include/h.h"
        timestamp = (self.old / "obj/a.o").stat().st_mtime_ns + 1000000000
        os.utime(header, ns=(timestamp, timestamp))
        self.generate()
        self.assertEqual(self.direct_compile(), 0)
        self.assertIn("stale donor input", self.receipt()["reason"])
        self.assert_miss(self.prepare(), "stale donor input")

    def test_donor_content_change_preserving_mtime_misses(self):
        self.ready()
        header = self.donor / "include/h.h"
        timestamp = header.stat().st_mtime_ns
        self.write(header, "#define VALUE 19\n")
        os.utime(header, ns=(timestamp, timestamp))
        self.generate()
        self.assertEqual(self.direct_compile(), 0)
        self.assertIn("stale donor input", self.receipt()["reason"])

    def test_complete_llvm_tree_must_match_and_remain_unchanged(self):
        resource = self.src / cache.CLANG / "lib/clang/fixture/include/test.h"
        original = resource.read_bytes()
        self.write(resource, "/* altered unused resource */\n")
        self.assert_miss(self.prepare(), "different complete tree")
        resource.write_bytes(original)
        self.ready()
        self.write(resource, "/* altered after prepare */\n")
        self.generate()
        self.assertEqual(self.direct_compile(), 0)
        self.assertIn("toolchain/sysroot content changed", self.receipt()["reason"])

    def test_traversal_absolute_paths_and_unsupported_flags_rejected(self):
        cases = [
            ["-o", "../escape.o"], ["-o", "/tmp/escape.o"],
            ["-I../../../escape"], ["-I/absolute/include"],
            ["@args.rsp"], ["-fmodules"], ["-include-pch", "gen/test.pch"],
            ["-fplugin=plugin.so"], ["-Xclang", "-load"], ["--coverage"],
            ["-fprofile-use=profile.profdata"], ["-gsplit-dwarf"], ["-ftime-trace"],
            ["-save-temps"], ["-Xclang", "-emit-pch"], ["-Wp,-include,hidden.h"],
            ["-Wa,-I,hidden"], ["-MJ", "other.json"],
        ]
        for flags in cases:
            with self.subTest(flags=flags), self.assertRaises(cache.Miss):
                cache.action(self.argv + flags, self.new, self.src)
        with self.assertRaisesRegex(cache.Miss, "compiler"):
            cache.action([str(CLANG), *self.argv[1:]], self.new, self.src)
        with self.assertRaisesRegex(cache.Miss, "traversal"):
            cache.relative_path("../../../escape", self.new, self.src)

    def test_prepare_never_executes_donor_commands(self):
        marker = self.work / "should-not-exist"
        with (self.old / "build.ninja").open("a") as stream:
            stream.write(f"rule bad\n  command = touch {marker}\nbuild bad: bad\n")
        run = subprocess.run
        calls = []
        def guarded(argv, **kwargs):
            calls.append(argv)
            self.assertIn(argv[0], ("/usr/bin/ninja", "/bin/ninja", "/usr/local/bin/ninja"))
            self.assertEqual(argv[1], "-t")
            return run(argv, **kwargs)
        with mock.patch.object(cache.subprocess, "run", side_effect=guarded):
            self.ready()
        self.assertTrue(calls)
        self.assertFalse(marker.exists())

    def test_external_dependency_and_external_symlink_rejected(self):
        outside = self.work / "external.h"
        self.write(outside, "#define EXTERNAL 1\n")
        source = f'#include "{outside}"\nint value(void){{return EXTERNAL;}}\n'
        self.write(self.donor / "a.c", source)
        self.run_ninja(self.old)
        self.assert_miss(self.prepare(), "external dependency")
        (self.src / "shadow/escape").symlink_to(self.work)
        with self.assertRaisesRegex(cache.Miss, "external symlink"):
            cache.action(self.argv + ["-I../../shadow/escape"], self.new, self.src)

    def test_system_headers_omitted_by_mmd_require_full_in_tree_sysroot(self):
        for root in (self.src, self.donor):
            self.write(root / "sysroot/usr/include/system.h", "#define SYSTEM 26\n")
            self.write(root / "a.c", '#include <system.h>\nint value(void){return SYSTEM;}\n')
        self.flags += ["--sysroot=../../sysroot", "-isystem../../sysroot/usr/include"]
        self.ninja_file(self.old, dependency_flag="-MMD")
        self.ninja_file(self.new, wrapped=True, dependency_flag="-MMD")
        self.run_ninja(self.old)
        self.donor_object = (self.old / "obj/a.o").read_bytes()
        self.argv = shlex.split(cache.compdb(self.old)[0]["command"])
        self.ready()
        self.run_ninja(self.new)
        self.assertEqual(self.receipt()["status"], "hit", self.receipt())
        self.assertIn("../../sysroot/usr/include/system.h",
                      cache.ninja_deps(self.new / ".ninja_deps")["obj/a.o"][1])
        self.write(self.src / "sysroot/usr/include/system.h", "#define SYSTEM 65\n")
        self.assertEqual(self.direct_compile(), 0)
        self.assertIn("toolchain/sysroot content changed", self.receipt()["reason"])

    def test_missing_date_time_warning_still_rejects_nondeterminism(self):
        self.flags.remove("-Werror=date-time")
        self.ninja_file(self.old)
        self.ninja_file(self.new, wrapped=True)
        self.run_ninja(self.old)
        self.donor_object = (self.old / "obj/a.o").read_bytes()
        self.argv = shlex.split(cache.compdb(self.old)[0]["command"])
        self.ready()
        self.generate()
        self.assertEqual(self.direct_compile(expect_hit=True), 0)
        for root in (self.src, self.donor):
            self.write(root / "a.c", 'const char *date = __DATE__;\n')
        self.run_ninja(self.old)
        self.donor_object = (self.old / "obj/a.o").read_bytes()
        self.ready()
        self.assertEqual(self.direct_compile(), 0)
        self.assertIn("time-dependent", self.receipt()["reason"])

    def test_malformed_metadata_and_unsupported_platform_disable_old_cache(self):
        self.ready()
        for platform in ("macos", "windows"):
            with self.subTest(platform=platform):
                self.assert_miss(cache.prepare(self.src, self.donor, platform, "x64", self.work),
                                 "unsupported platform")
                self.assertEqual(json.loads((self.work / cache.CACHE / "manifest.json").read_text())["status"], "miss")
        path = self.old / ".ninja_deps"
        original = path.read_bytes()
        for data in (b"junk", original[:-1], original[:16] + struct.pack("<I", 0xffffffff)):
            with self.subTest(data=data[:20]):
                path.write_bytes(data)
                self.assert_miss(self.prepare(), "Ninja")
        path.write_bytes(original)
        self.write(self.old / ".ninja_log", "# ninja log v4\n")
        self.assert_miss(self.prepare(), "requires v5")

    def test_preprocessor_timeout_falls_back_and_failure_propagates(self):
        self.ready()
        self.generate()
        with mock.patch.object(cache.subprocess, "run", side_effect=subprocess.TimeoutExpired("clang", 120)):
            self.assertEqual(self.direct_compile(), 0)
        self.assertIn("timed out", self.receipt()["reason"])
        self.write(self.src / "a.c", "this is not valid C;\n")
        self.assertNotEqual(self.direct_compile(), 0)
        self.assertNotEqual(self.receipt()["returncode"], 0)

    def test_staged_object_integrity_and_owned_donor_lifetime(self):
        self.ready()
        manifest = json.loads((self.work / cache.CACHE / "manifest.json").read_text())
        self.assertTrue(manifest["donor"]["relative"])
        (self.old / "obj/a.o").unlink()
        self.generate()
        self.assertEqual(self.direct_compile(expect_hit=True), 0)
        payload = self.work / cache.CACHE / manifest["generation"] / f"{cache.key('obj/a.o')}.o"
        payload.write_bytes(b"not an object")
        self.assertEqual(self.direct_compile(), 0)
        self.assertIn("integrity mismatch", self.receipt()["reason"])
        shutil.rmtree(self.donor)
        self.assertEqual(self.direct_compile(), 0)
        self.assertEqual(self.receipt()["status"], "miss")

    def test_real_cxx_hit_executes_only_canonical_preprocessor(self):
        for root in (self.src, self.donor):
            self.write(root / "a.cc", 'template<int N> int f(){return N;}\nint v(){return f<42>();}\n')
        self.compiler = f"../../{cache.CLANG}/bin/clang++"
        self.flags += ["-std=c++17", "-fno-exceptions", "-fno-rtti", "-Werror",
                       "-fcrash-diagnostics-dir=../clang-crashreports"]
        for directory, wrapped in ((self.old, False), (self.new, True)):
            self.ninja_file(directory, wrapped=wrapped)
            path = directory / "build.ninja"
            path.write_text(path.read_text().replace("rule cc\n", "rule cxx\n")
                            .replace(": cc ../../a.c", ": cxx ../../a.cc"))
        self.run_ninja(self.old)
        self.donor_object = (self.old / "obj/a.o").read_bytes()
        self.argv = shlex.split(cache.compdb(self.old)[0]["command"])
        self.ready()
        self.generate()
        run = subprocess.run
        invocations = []
        def tracked(argv, **kwargs):
            invocations.append((argv, kwargs))
            return run(argv, **kwargs)
        with mock.patch.object(cache.subprocess, "run", side_effect=tracked):
            self.assertEqual(self.direct_compile(expect_hit=True), 0)
        self.assertEqual(len(invocations), 2)
        for argv, options in invocations:
            self.assertIn("-E", argv)
            self.assertNotIn("-c", argv)
            self.assertEqual(options["executable"], str((self.src / cache.CLANG / "bin/clang").resolve()))
            self.assertEqual(options["timeout"], cache.PREPROCESS_TIMEOUT)
        self.assertEqual((self.new / "obj/a.o").read_bytes(), self.donor_object)

    def test_packaged_clang_resource_headers_roundtrip(self):
        resource = subprocess.check_output([str(CLANG), "-print-resource-dir"], text=True).strip()
        version = Path(resource).name
        for root in (self.src, self.donor):
            self.write(root / cache.CLANG / f"lib/clang/{version}/include/resource.h", "#define RESOURCE 81\n")
            self.write(root / "a.c", '#include <resource.h>\nint value(void){return RESOURCE;}\n')
        self.flags.remove("-nostdinc")
        self.flags += ["-nostdlibinc", "-no-canonical-prefixes"]
        self.ninja_file(self.old, dependency_flag="-MMD")
        self.ninja_file(self.new, wrapped=True, dependency_flag="-MMD")
        self.run_ninja(self.old)
        self.donor_object = (self.old / "obj/a.o").read_bytes()
        self.argv = shlex.split(cache.compdb(self.old)[0]["command"])
        self.ready()
        self.run_ninja(self.new)
        self.assertEqual(self.receipt()["status"], "hit", self.receipt())

    def test_parallel_objects_have_independent_receipts(self):
        for root in (self.src, self.donor):
            self.write(root / "b.c", "int other(void){return 64;}\n")
        for directory in (self.old, self.new):
            with (directory / "build.ninja").open("a") as stream:
                stream.write("build obj/b.o: cc ../../b.c || gen/generated.h\ndefault obj/b.o\n")
        self.run_ninja(self.old, "-j2")
        report = self.prepare()
        self.assertEqual(report["status"], "ready", report)
        self.assertEqual(report["counts"]["prepared"], 2)
        self.run_ninja(self.new, "-j2")
        receipts = list((self.work / cache.CACHE / "receipts").glob("*.json"))
        self.assertEqual(len(receipts), 2)
        for path in receipts:
            receipt = json.loads(path.read_text())
            self.assertEqual(receipt["status"], "hit", receipt)
        self.assertIn("no work to do", self.run_ninja(self.new))

    def test_poisoned_token_pasted_date_cannot_suppress_warning(self):
        self.flags.remove("-Werror=date-time")
        source = ('#pragma clang diagnostic ignored "-Wdate-time"\n'
                  '#define CONCAT(a,b) a##b\nconst char *date = CONCAT(__DA,TE__);\n')
        for root in (self.src, self.donor):
            self.write(root / "a.c", source)
        self.ninja_file(self.old)
        self.ninja_file(self.new, wrapped=True)
        self.run_ninja(self.old)
        self.donor_object = (self.old / "obj/a.o").read_bytes()
        self.argv = shlex.split(cache.compdb(self.old)[0]["command"])
        self.ready()
        self.generate()
        self.assertEqual(self.direct_compile(), 0)
        self.assertIn("preprocessor failed", self.receipt()["reason"])

    def archive_donor(self):
        archive = self.work / "donor.tar"
        environment = {name: value for name, value in os.environ.items() if name != "TAR_OPTIONS"}
        subprocess.run(["tar", "-cf", str(archive), "-C", str(self.donor), "."],
                       check=True, env=environment)
        shutil.rmtree(self.donor)
        self.donor.mkdir()
        subprocess.run(["tar", "-xf", str(archive), "-C", str(self.donor)],
                       check=True, env=environment)

    def older_donor_inputs(self):
        cutoff = cache.ninja_log(self.old / ".ninja_log")["obj/a.o"][0]
        old = cutoff // cache.NANOSECOND * cache.NANOSECOND - 5 * cache.NANOSECOND
        for path in self.donor.rglob("*"):
            if path.is_file() and not path.is_symlink() and path != self.old / "obj/a.o":
                os.utime(path, ns=(old, old))
        return old

    @unittest.skipUnless(shutil.which("tar"), "GNU tar required")
    def test_plain_tar_requires_opt_in_and_old_inputs_for_real_hit(self):
        self.older_donor_inputs()
        exact_mtime = (self.old / "obj/a.o").stat().st_mtime_ns
        self.assertNotEqual(exact_mtime % cache.NANOSECOND, 0)
        self.archive_donor()
        actual = (self.old / "obj/a.o").stat().st_mtime_ns
        self.assertEqual(actual, exact_mtime // cache.NANOSECOND * cache.NANOSECOND)
        self.assert_miss(self.prepare(), "stale Ninja object/dependency mtime")
        self.ready(allow_truncated_mtimes=True)
        manifest = json.loads((self.work / cache.CACHE / "manifest.json").read_text())
        record = json.loads((self.work / cache.CACHE / manifest["generation"] /
                             f"{cache.key('obj/a.o')}.json").read_text())
        self.assertEqual(record["object_mtime"], actual)
        self.assertEqual(record["dependency_mtime"], exact_mtime)
        self.assertEqual(record["freshness_cutoff"],
                         record["log_mtime"] // cache.NANOSECOND * cache.NANOSECOND)
        self.run_ninja(self.new)
        self.assertEqual(self.receipt()["status"], "hit", self.receipt())
        self.assertEqual((self.new / "obj/a.o").read_bytes(), self.donor_object)
        self.assertIn("no work to do", self.run_ninja(self.new))

    @unittest.skipUnless(shutil.which("tar"), "GNU tar required")
    def test_coarse_same_second_and_newer_inputs_miss_prepare_and_runtime(self):
        self.older_donor_inputs()
        self.archive_donor()
        cutoff = cache.ninja_log(self.old / ".ninja_log")["obj/a.o"][0]
        cutoff = cutoff // cache.NANOSECOND * cache.NANOSECOND
        self.generate()
        for relative in ("a.c", "include/h.h", "out/Default/gen/generated.h",
                         str(cache.CLANG / "lib/clang/fixture/include/test.h")):
            for timestamp in (cutoff, cutoff + cache.NANOSECOND):
                with self.subTest(relative=relative, timestamp=timestamp):
                    path = self.donor / relative
                    old = path.stat().st_mtime_ns
                    self.ready(allow_truncated_mtimes=True)
                    os.utime(path, ns=(timestamp, timestamp))
                    self.assertEqual(self.direct_compile(), 0)
                    self.assertIn("stale donor", self.receipt()["reason"])
                    self.assert_miss(self.prepare(allow_truncated_mtimes=True), "stale donor")
                    os.utime(path, ns=(old, old))

    def test_zlib_like_c_module_metadata_allowed_but_modules_still_miss(self):
        options = ["-fmodule-name=//third_party/zlib:zlib_Private",
                   "-Xclang", "-fmodule-file-home-is-cwd",
                   "-Xclang", "-fmodules-cache-path=/not_exist_dummy_dir"]
        argv = self.argv + options
        self.assertEqual(cache.action(argv, self.new, self.src)["source"], "../../a.c")
        for extra in (["-fmodules"], ["-fcxx-modules"], ["-fmodule-file=gen/test.pcm"],
                      ["-fmodule-map-file=gen/module.modulemap"],
                      ["-Xclang", "-emit-module"], ["-Xclang", "-load"],
                      ["-Xclang", "-fmodules-cache-path=/somewhere_else"],
                      ["-fmodule-name=/absolute/path"], ["--sysroot=/absolute"]):
            with self.subTest(extra=extra), self.assertRaises(cache.Miss):
                cache.action(argv + extra, self.new, self.src)
        with self.assertRaisesRegex(cache.Miss, "non-module C"):
            cache.action([self.compiler + "++", *argv[1:]], self.new, self.src)
        probe = subprocess.run([str(CLANG), *options, "-fsyntax-only", "-x", "c", "-"],
                               input="int x;\n", text=True, capture_output=True)
        if probe.returncode:
            self.skipTest("local Clang lacks inert module metadata options: " + probe.stderr)
        self.flags += options
        self.ninja_file(self.old)
        self.ninja_file(self.new, wrapped=True)
        self.run_ninja(self.old)
        self.donor_object = (self.old / "obj/a.o").read_bytes()
        self.argv = shlex.split(cache.compdb(self.old)[0]["command"])
        self.ready()
        self.run_ninja(self.new)
        self.assertEqual(self.receipt()["status"], "hit", self.receipt())
        self.assertEqual((self.new / "obj/a.o").read_bytes(), self.donor_object)
        self.assertIn("no work to do", self.run_ninja(self.new))

    def test_llvm_and_sysroot_directory_aliases_real_hit(self):
        for root in (self.src, self.donor):
            resource = root / cache.CLANG / "lib/clang/23/lib"
            self.write(resource / "x86_64-unknown-linux-gnu/runtime.a", b"archive")
            (resource / "x86_64-cros-linux-gnu").symlink_to("x86_64-unknown-linux-gnu")
            include = root / "sysroot/usr/include"
            self.write(include / "libpng16/png.h", "#define PNG 16\n")
            (include / "libpng").symlink_to("libpng16")
            self.write(root / "a.c", '#include <libpng/png.h>\nint value(void){return PNG;}\n')
        self.flags += ["--sysroot=../../sysroot", "-isystem../../sysroot/usr/include"]
        self.ninja_file(self.old, dependency_flag="-MMD")
        self.ninja_file(self.new, wrapped=True, dependency_flag="-MMD")
        self.run_ninja(self.old)
        self.donor_object = (self.old / "obj/a.o").read_bytes()
        self.argv = shlex.split(cache.compdb(self.old)[0]["command"])
        self.ready()
        self.run_ninja(self.new)
        self.assertEqual(self.receipt()["status"], "hit", self.receipt())
        self.assertIn("no work to do", self.run_ninja(self.new))
        alias = self.donor / "sysroot/usr/include/libpng"
        alias.unlink()
        alias.symlink_to("../include/libpng16")
        self.assertEqual(self.direct_compile(), 0)
        self.assertIn("symlink changed", self.receipt()["reason"])

    def test_work_relocation_preserves_relative_cache(self):
        self.ready()
        relocated = self.work.parent / "relocated"
        self.work.rename(relocated)
        self.work = relocated
        self.src = relocated / "src"
        self.new = self.src / "out/Chromix"
        self.donor = relocated / "upstream-cache/src"
        self.old = self.donor / "out/Default"
        self.run_ninja(self.new)
        self.assertEqual(self.receipt()["status"], "hit", self.receipt())


if __name__ == "__main__":
    unittest.main()
