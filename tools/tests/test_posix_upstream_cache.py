"""Test optional cache wiring without downloading or compiling Chromium."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
BASH32 = Path.home() / ".local/bash-3.2-for-ci/bash"


class PosixUpstreamCacheTest(unittest.TestCase):
    def test_all_five_targets_receive_cache_option(self):
        caller = (REPO / ".github/workflows/build-cross-platform.yml").read_text()
        self.assertEqual(caller.count("use_upstream_cache: ${{"), 5)
        self.assertIn("'tools/*upstream_cache.py'", caller)
        workflow = (REPO / ".github/workflows/build-posix-github.yml").read_text()
        self.assertIn("UPSTREAM_ACTIONS_TOKEN:", workflow)
        self.assertIn("secrets.UPSTREAM_ACTIONS_TOKEN || github.token", workflow)
        self.assertIn("upstream-cache-import.json", workflow)
        self.assertIn("upstream-cache-plan.log", workflow)

    def test_restore_precedes_source_preparation_only_on_fresh_first_stage(self):
        stage = (REPO / "build/posix/ci-stage.sh").read_text()
        self.assertLess(stage.index('"$REPO/build/posix/fetch-upstream-cache.sh"'),
                        stage.index('"$REPO/tools/restore_upstream_cache.py"'))
        self.assertLess(stage.index('"$REPO/tools/restore_upstream_cache.py"'),
                        stage.index('"$REPO/build/prepare-ungoogled.sh"'))
        self.assertIn('[ "$STAGE_INDEX" -eq 1 ] && [ -z "$FROM_SNAPSHOT" ]', stage)
        self.assertIn('[ "$(remaining_min)" -ge "$CACHE_REQUIRED_MINUTES" ]', stage)
        self.assertIn('[ ! -e "$SRC" ]', stage)
        self.assertNotIn('export CHROMIX_UPSTREAM_CACHE_DIR=', stage)

    def test_builders_keep_restored_output_and_regenerate_chromix_graph(self):
        for platform, script in (("linux", "build/build.sh"), ("macos", "build/macos/build.sh")):
            with self.subTest(platform=platform):
                source = (REPO / script).read_text()
                self.assertLess(source.index('utils/domain_substitution.py" apply'),
                                source.index('"$OUT/gn" gen'))
                self.assertLess(source.index('"$OUT/gn" gen'),
                                source.index("chromix_report_upstream_plan"))
                self.assertIn('OUT="$SRC/out/Chromix"', source)
                self.assertIn('OUT="$SRC/out/Default"', source)
                self.assertIn('.chromix-upstream-restored.json', source)
                self.assertNotIn('chromix_import_upstream_cache objects', source)
                self.assertNotIn('chromix_configure_upstream_objects', source)
                self.assertIn('"$REPO/tools/merge_gn_args.py"', source)
                self.assertIn('--fail-on-unused-args', source)

    def run_restored_builder(self, platform, arch, *, fail_tools=False):
        with tempfile.TemporaryDirectory(prefix="restored build ") as directory:
            root = Path(directory)
            repo, work, binaries = root / "repo", root / "work", root / "bin"
            src, out = work / "src", work / "src/out/Default"
            out.mkdir(parents=True)
            binaries.mkdir()
            log = root / "calls"

            def script(path, body):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n" + body)
                path.chmod(0o755)

            builder = "build/build.sh" if platform == "linux" else "build/macos/build.sh"
            for relative in (builder, "build/posix/upstream-cache.sh", "tools/merge_gn_args.py"):
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO / relative, target)
            # Selection is explicit here; these shell fixtures do not contain native binaries or real logs.
            (repo / "tools/restore_ninja.py").write_text(f'''import os, sys
from pathlib import Path
assert sys.argv[1:] == ['--workdir', {str(work)!r}, '--platform', {platform!r}, '--arch', {arch!r}]
assert (Path({str(src)!r}) / '.chromix-upstream-restored.json').is_file()
with open(os.environ['CALL_LOG'], 'a') as output:
    output.write('ninja-guard\\n')
print(os.environ['SELECTED_NINJA'])
''')
            script(repo / "build/prepare-ungoogled.sh", 'printf "prepare\\n" >> "$CALL_LOG"\n')
            script(repo / "build/posix/prepare-restored-tools.sh",
                   'printf "tools\\n" >> "$CALL_LOG"\n' +
                   ('exit 19\n' if fail_tools else 'touch "$1/src/.chromix-toolchain-ready"\n'))
            script(repo / "build/macos/select-xcode.sh", 'select_macos_xcode() { :; }\n')
            for name in (".chromix-upstream-restored.json", ".chromix-domain-substituted"):
                (src / name).touch()
            overlay = "args.gn" if platform == "linux" else "args.macos.gn"
            (repo / "build" / overlay).write_text('symbol_level = 0\nchrome_pgo_phase = 0\n')
            platform_repo = "ungoogled-chromium-" + ("portablelinux" if platform == "linux" else "macos")
            for name, flag in (("ungoogled-chromium", "flags.gn"), (platform_repo, f"flags.{platform}.gn")):
                path = work / "tooling" / name / flag
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('symbol_level = 1\n')
            (out / "args.gn").write_text('symbol_level = 2\nchrome_pgo_phase = 2\nupstream_extra = true\n')
            for name in (".ninja_deps", ".ninja_log", "build.ninja", "retained.o"):
                (out / name).write_text(name)
            script(out / "gn", 'printf "gn\\n" >> "$CALL_LOG"\n')
            script(out / "chrome", 'printf "Chromium fixture\\n"\n')
            for name in ("node", "go", "gperf", "clang-format"):
                script(binaries / name, "exit 0\n")
            selected_ninja = root / "selected tools/ninja"
            script(binaries / "ninja", 'printf "unselected-ninja\\n" >> "$CALL_LOG"\nexit 97\n')
            script(selected_ninja, 'test "$NINJA" = "$0" || exit 98\n'
                   'printf "ninja\\n" >> "$CALL_LOG"\n')
            machine = "x86_64" if arch == "x64" else ("aarch64" if platform == "linux" else "arm64")
            system = "Linux" if platform == "linux" else "Darwin"
            script(binaries / "uname", f'case "$1" in -m) printf "{machine}\\n";; -s) printf "{system}\\n";; esac\n')
            script(binaries / "sysctl", 'printf "2\\n"\n')
            env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                   "CHROMIX_SKIP_DEPS": "1", "CHROMIX_JOBS": "2", "CALL_LOG": str(log),
                   "SELECTED_NINJA": str(selected_ninja)}
            env.pop("CHROMIX_UPSTREAM_CACHE_DIR", None)
            for _ in range(1 if fail_tools else 2):
                result = subprocess.run([str(BASH32 if BASH32.exists() else shutil.which("bash")),
                                         str(repo / builder), str(work), arch],
                                        env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 19 if fail_tools else 0, result.stdout + result.stderr)
            if fail_tools:
                self.assertEqual(log.read_text().splitlines(), ["prepare", "ninja-guard", "tools"])
            else:
                self.assertEqual(log.read_text().splitlines(),
                                 ["prepare", "ninja-guard", "tools", "gn", "ninja", "ninja"] * 2)
                args = (out / "args.gn").read_text()
                self.assertIn("upstream_extra = true", args)
                self.assertIn("symbol_level = 0", args)
                self.assertIn("chrome_pgo_phase = 0", args)
                self.assertIn(f'target_cpu = "{arch}"', args)
                self.assertNotIn("symbol_level = 2", args)
            for name in (".ninja_deps", ".ninja_log", "build.ninja", "retained.o"):
                self.assertEqual((out / name).read_text(), name)
            self.assertFalse((src / "out/Chromix").exists())

    def test_four_restored_builders_prepare_tools_on_every_resume_and_keep_args(self):
        for platform in ("linux", "macos"):
            for arch in ("x64", "arm64"):
                with self.subTest(platform=platform, arch=arch):
                    self.run_restored_builder(platform, arch)

    def test_restored_tool_failure_stops_before_gn_and_ninja(self):
        for platform in ("linux", "macos"):
            with self.subTest(platform=platform):
                self.run_restored_builder(platform, "x64", fail_tools=True)

    def run_helper(self, bash, enabled):
        with tempfile.TemporaryDirectory(prefix="cache shell ") as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / "tools").mkdir(parents=True)
            log = root / "args"
            (repo / "tools/import_upstream_cache.py").write_text(
                'import os, sys\nfrom pathlib import Path\n'
                'Path(os.environ["TEST_CACHE_LOG"]).write_text("\\n".join(sys.argv[1:]))\n')
            env = {**os.environ, "REPO": str(repo), "WORK": str(root / "work"),
                   "ARCH": "arm64", "TEST_CACHE_LOG": str(log)}
            env.pop("CHROMIX_UPSTREAM_CACHE_DIR", None)
            if enabled:
                env["CHROMIX_UPSTREAM_CACHE_DIR"] = str(root / "cache with spaces")
            result = subprocess.run(
                [str(bash), "-euo", "pipefail", "-c",
                 'source "$1"; chromix_import_upstream_cache toolchain linux',
                 "fixture", str(REPO / "build/posix/upstream-cache.sh")],
                capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if enabled:
                self.assertEqual(log.read_text().splitlines(), [
                    "--phase", "toolchain", "--platform", "linux", "--arch", "arm64",
                    "--workdir", str(root / "work"), "--cache-dir", str(root / "cache with spaces")])
            else:
                self.assertFalse(log.exists())

    def test_object_wrapper_survives_stage_resume_without_donor_environment(self):
        with tempfile.TemporaryDirectory(prefix="object wrapper ") as directory:
            root = Path(directory)
            out = root / "src/out/Chromix"
            out.mkdir(parents=True)
            args = out / "args.gn"
            (root / "cache").mkdir()
            (root / "cache/result.json").write_text(json.dumps({"status": "hit", "extraction_scope": "source-and-objects"}))
            env = {**os.environ, "REPO": str(root / "repo with spaces"),
                   "WORK": str(root), "OUT": str(out), "CHROMIX_UPSTREAM_CACHE_DIR": str(root / "cache")}
            for resumed in (False, True):
                if resumed:
                    env.pop("CHROMIX_UPSTREAM_CACHE_DIR")
                args.write_text('is_debug = false\n')
                result = subprocess.run(
                    [str(BASH32 if BASH32.exists() else shutil.which("bash")), "-euo", "pipefail", "-c",
                     'source "$1"; chromix_configure_upstream_objects', "fixture",
                     str(REPO / "build/posix/upstream-cache.sh")],
                    env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = args.read_text().splitlines()
                self.assertEqual(lines[0], "is_debug = false")
                command = json.loads(lines[1].split(" = ", 1)[1])
                self.assertEqual(command, "python3 '" + str(root / "repo with spaces/tools/upstream_object_cache.py") + "' compile --")
                self.assertTrue((root / ".chromix-object-wrapper").is_file())

    def test_failed_download_does_not_enable_compiler_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "src/out/Chromix"
            out.mkdir(parents=True)
            (out / "args.gn").write_text('is_debug = false\n')
            result = subprocess.run(
                [shutil.which("bash"), "-euo", "pipefail", "-c",
                 'source "$1"; chromix_configure_upstream_objects', "fixture",
                 str(REPO / "build/posix/upstream-cache.sh")],
                env={**os.environ, "WORK": str(root), "OUT": str(out), "REPO": str(REPO),
                     "CHROMIX_UPSTREAM_CACHE_DIR": str(root / "absent")},
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("cc_wrapper", (out / "args.gn").read_text())
            self.assertFalse((root / ".chromix-object-wrapper").exists())

    def test_restore_diagnostic_is_finalized_before_handoff(self):
        source = (REPO / "build/posix/ci-stage.sh").read_text()
        self.assertIn("upstream-cache-restore.json", (REPO / ".github/workflows/build-posix-github.yml").read_text())
        self.assertLess(source.index("--phase restore"), source.index('"$TIMEOUT" -k 7m'))
        self.assertIn(".chromix-upstream-restored.json", source)

    def test_disabled_cache_is_noop(self):
        self.run_helper(shutil.which("bash"), False)

    def test_cache_paths_are_quoted(self):
        self.run_helper(shutil.which("bash"), True)

    @unittest.skipUnless(BASH32.exists(), "Bash 3.2 required")
    def test_bash32_enabled_and_disabled(self):
        self.run_helper(BASH32, False)
        self.run_helper(BASH32, True)


if __name__ == "__main__":
    unittest.main()
