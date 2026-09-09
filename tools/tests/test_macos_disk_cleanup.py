"""Tiny macOS cleanup fixtures; no privileged commands, artifacts, or builds."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "build/macos/free-disk-space.py"
WORKFLOW = REPO / ".github/workflows/build-posix-github.yml"
BASH32 = Path.home() / ".local/bash-3.2-for-ci/bash"
spec = importlib.util.spec_from_file_location("macos_disk_cleanup", HELPER)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class MacOSDiskCleanupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mac disk fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.apps = self.root / "Applications"
        self.apps.mkdir()
        self.runtimes = self.root / "Library/Developer/CoreSimulator/Profiles/Runtimes"
        self.selected = self.xcode("Xcode_26.app")
        self.developer = self.selected / "Contents/Developer"
        self.sdk = self.developer / "Platforms/MacOSX.platform/Developer/SDKs/MacOSX26.sdk"
        self.sdk.mkdir(parents=True)
        (self.sdk / "keep").write_text("macOS SDK")
        self.env = {"CI": "true", "GITHUB_ACTIONS": "true", "RUNNER_OS": "macOS",
                    "RUNNER_ENVIRONMENT": "github-hosted", "DEVELOPER_DIR": str(self.developer),
                    "PATH": "/usr/bin:/bin"}
        for variable in ("HOME", "RUNNER_TEMP", "GITHUB_WORKSPACE", "RUNNER_WORKSPACE", "RUNNER_TOOL_CACHE"):
            path = self.root / variable
            path.mkdir()
            (path / "keep").write_text(variable)
            self.env[variable] = str(path)
        self.free = 42
        self.gain = 100
        self.removed = []
        self.commands = []
        self.sdk_output = str(self.sdk)
        self.tool_outputs = {tool: str(self.developer / "Toolchains/XcodeDefault.xctoolchain/usr/bin" / tool)
                             for tool in ("clang", "ld")}
        self.system = "Darwin"
        self.fail_remove = False
        for name, value in (("APPLICATIONS", self.apps), ("SIMULATOR_RUNTIMES", self.runtimes),
                            ("CLEANUP_TARGET_BYTES", 120)):
            patcher = mock.patch.object(cleanup, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def xcode(self, name, parent=None):
        path = (parent or self.apps) / name
        (path / "Contents/Developer/Platforms/MacOSX.platform").mkdir(parents=True)
        (path / "keep").write_text(name)
        binaries = path / "Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin"
        binaries.mkdir(parents=True)
        for tool in ("clang", "ld"):
            binary = binaries / tool
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
        return path

    def runtime(self, name="iOS 26.simruntime", parent=None):
        path = (parent or self.runtimes) / name
        path.mkdir(parents=True)
        (path / "payload").write_text("tiny")
        return path

    def mobile_sdk(self, name="iPhoneOS"):
        return self.runtime(name + "26.sdk", self.developer / "Platforms" /
                            (name + ".platform") / "Developer/SDKs")

    def fake_run(self, command, **kwargs):
        self.assertTrue(kwargs.get("check"))
        self.commands.append(command)
        if command[0] == "df":
            self.assertEqual(command, ["df", "-h", str(Path(self.env["RUNNER_TEMP"]).resolve())])
        else:
            self.assertEqual(command[:-1], ["sudo", "-n", "/bin/rm", "-rf", "-x", "--"])
            path = Path(command[-1])
            self.assertIn(self.root, path.parents)
            self.assertFalse(path.is_symlink())
            if self.fail_remove:
                raise subprocess.CalledProcessError(1, command)
            shutil.rmtree(path)
            self.removed.append(path)
            self.free += self.gain
        return subprocess.CompletedProcess(command, 0)

    def fake_xcrun(self, command, **kwargs):
        self.assertEqual(kwargs, {"text": True})
        self.assertEqual(command[:3], ["/usr/bin/xcrun", "--sdk", "macosx"])
        if command[3:] == ["--show-sdk-path"]:
            return self.sdk_output
        self.assertIn(command[3:], (["--find", "clang"], ["--find", "ld"]))
        return self.tool_outputs[command[-1]]

    def run_cleanup(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(cleanup.platform, "system", return_value=self.system), \
                mock.patch.object(cleanup.shutil, "disk_usage", side_effect=lambda _: mock.Mock(free=self.free)), \
                mock.patch.object(cleanup.subprocess, "check_output", side_effect=self.fake_xcrun) as xcrun, \
                mock.patch.object(cleanup.subprocess, "run", side_effect=self.fake_run), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = cleanup.main()
        self.xcrun_calls = xcrun.call_count
        return status, output.getvalue()

    def assert_selected_preserved(self):
        self.assertEqual((self.sdk / "keep").read_text(), "macOS SDK")
        self.assertEqual((self.selected / "keep").read_text(), "Xcode_26.app")
        for variable in ("HOME", "RUNNER_TEMP", "GITHUB_WORKSPACE", "RUNNER_WORKSPACE", "RUNNER_TOOL_CACHE"):
            self.assertEqual((self.root / variable / "keep").read_text(), variable)

    def test_selected_symlink_is_preserved_and_unused_real_bundle_removed(self):
        alias = self.apps / "Xcode.app"
        alias.symlink_to(self.selected, target_is_directory=True)
        self.env["DEVELOPER_DIR"] = str(alias / "Contents/Developer")
        old = self.xcode("Xcode_16.app")
        (old / "selected-link").symlink_to(self.selected, target_is_directory=True)
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.removed, [old])
        self.assertTrue(alias.is_symlink())
        self.assert_selected_preserved()
        for message in ("before:", "after:", "free_bytes=42", "free_bytes=142",
                        "reclaimed_bytes=100", "canonical=", "macOS SDK=", "restore"):
            self.assertIn(message, output)
        self.assertEqual(sum(command[0] == "df" for command in self.commands), 2)

    def test_selected_symlink_still_allows_canonical_mobile_sdk_cleanup(self):
        alias = self.apps / "Xcode.app"
        alias.symlink_to(self.selected, target_is_directory=True)
        self.env["DEVELOPER_DIR"] = str(alias / "Contents/Developer")
        sdk = self.mobile_sdk()
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.removed, [sdk])
        self.assertTrue(alias.is_dir())
        self.assert_selected_preserved()

    def test_selected_intermediate_symlinks_inside_other_bundles_are_kept(self):
        carrier = self.xcode("Xcode_carrier.app")
        link = carrier / "Contents/Link.app"
        link.symlink_to("../../" + self.selected.name, target_is_directory=True)
        alias = self.apps / "Xcode.app"
        alias.symlink_to(link, target_is_directory=True)
        self.env["DEVELOPER_DIR"] = str(alias / "Contents/Developer")
        old = self.xcode("Xcode_unused.app")
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.removed, [old])
        self.assertTrue(carrier.exists())
        self.assertTrue(alias.is_dir())
        self.assert_selected_preserved()

    def test_selected_developer_directory_link_keeps_carrier_and_destination(self):
        carrier = self.xcode("Xcode_carrier.app")
        shutil.rmtree(carrier / "Contents/Developer")
        (carrier / "Contents/Developer").symlink_to(self.developer, target_is_directory=True)
        self.env["DEVELOPER_DIR"] = str(carrier / "Contents/Developer")
        old = self.xcode("Xcode_unused.app")
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.removed, [old])
        self.assertTrue(carrier.exists())
        self.assert_selected_preserved()

    def test_sufficient_space_is_noop_and_does_not_inspect_removal_roots(self):
        old = self.xcode("Xcode_16.app")
        self.free = 120
        with mock.patch.object(cleanup, "candidates", side_effect=AssertionError("unnecessary cleanup")):
            status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertTrue(old.exists())
        self.assertFalse(self.removed)
        self.assertIn("net_reclaimed_bytes=0", output)
        self.assertIn("capacity is not guaranteed", output)

    def test_runtime_then_mobile_sdk_only_if_bundle_cleanup_is_insufficient(self):
        old = self.xcode("Xcode_16.app", self.apps / "Xcodes")
        runtime = self.runtime()
        sdk = self.mobile_sdk()
        unneeded = self.mobile_sdk("AppleTVOS")
        self.gain = 26
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.removed, [old, runtime, sdk])
        self.assertTrue(unneeded.exists())
        self.assert_selected_preserved()

    def test_runtime_and_mobile_sdks_kept_if_unused_xcode_is_enough(self):
        old = self.xcode("Xcode_16.app")
        runtime = self.runtime()
        sdk = self.mobile_sdk()
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.removed, [old])
        self.assertTrue(runtime.exists())
        self.assertTrue(sdk.exists())

    def test_unsafe_candidates_and_unlisted_paths_are_never_removed(self):
        outside = self.xcode("Outside.app", self.root)
        (self.apps / "Xcode_unsafe.app").symlink_to(outside, target_is_directory=True)
        (self.apps / "Xcode_broken.app").symlink_to(self.root / "missing")
        (self.apps / "Xcode_file.app").write_text("not a bundle")
        (self.apps / "Xcode_fake.app").mkdir()
        (self.apps / "Other.app").mkdir()
        self.runtimes.mkdir(parents=True)
        (self.runtimes / "iOS unsafe.simruntime").symlink_to(outside, target_is_directory=True)
        (self.runtimes / "macOS 26.simruntime").mkdir()
        (self.runtimes / "unknown.simruntime").mkdir()
        sdks = self.mobile_sdk().parent
        shutil.rmtree(sdks / "iPhoneOS26.sdk")
        (sdks / "iPhoneOS26.sdk").symlink_to(self.sdk, target_is_directory=True)
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertFalse(self.removed)
        self.assertIn("refused unsafe target", output)
        self.assertIn("refused non-Xcode bundle", output)
        self.assertTrue((outside / "keep").exists())
        self.assert_selected_preserved()

    def test_symlinked_allowlist_parent_cannot_escape(self):
        outside = self.root / "elsewhere"
        old = self.xcode("Xcode_16.app", outside)
        (self.apps / "Xcodes").symlink_to(outside, target_is_directory=True)
        self.runtimes.parent.mkdir(parents=True)
        self.runtimes.symlink_to(outside, target_is_directory=True)
        mobile = self.developer / "Platforms/iPhoneOS.platform"
        mobile.symlink_to(outside, target_is_directory=True)
        self.runtime("iPhoneOS26.sdk", outside / "Developer/SDKs")
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertFalse(self.removed)
        self.assertTrue((old / "keep").exists())
        self.assertIn("refused unsafe allowlist directory", output)

    def test_selected_outside_allowlist_and_alias_are_preserved(self):
        custom = self.xcode("Custom.app", self.root)
        self.env["DEVELOPER_DIR"] = str(custom / "Contents/Developer")
        (self.apps / "Xcode_custom.app").symlink_to(custom, target_is_directory=True)
        custom_mobile = self.runtime("iPhoneOS26.sdk", custom / "Contents/Developer/Platforms/"
                                     "iPhoneOS.platform/Developer/SDKs")
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertFalse(self.removed)
        self.assertTrue(custom_mobile.exists())
        self.assertTrue((custom / "keep").exists())

    def test_runner_workdir_tools_and_external_sdk_are_protected(self):
        for variable in ("HOME", "RUNNER_TEMP", "GITHUB_WORKSPACE", "RUNNER_WORKSPACE", "RUNNER_TOOL_CACHE"):
            old = self.xcode("Xcode_" + variable + ".app")
            nested = old / "protected"
            nested.mkdir()
            self.env[variable] = str(nested)
        tools = self.xcode("Xcode_tools.app")
        self.env["PATH"] += os.pathsep + str(tools / "Contents/Developer/usr/bin")
        external = self.xcode("Xcode_external_sdk.app")
        self.sdk_output = str(external / "Contents/Developer/Platforms/MacOSX.platform")
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertFalse(self.removed)
        self.assertIn("refused protected target", output)

    def test_protected_paths_preserve_symlink_carrier_bundles(self):
        for variable in ("RUNNER_TEMP", "RUNNER_WORKSPACE", "RUNNER_TOOL_CACHE"):
            carrier = self.xcode("Xcode_" + variable + ".app")
            alias = carrier / "alias"
            alias.symlink_to(self.env[variable], target_is_directory=True)
            self.env[variable] = str(alias)
        tools = self.xcode("Xcode_tools.app")
        (tools / "bin").symlink_to(self.root / "HOME", target_is_directory=True)
        self.env["PATH"] += os.pathsep + str(tools / "bin")
        external = self.xcode("Xcode_external_sdk.app")
        (external / "sdk").symlink_to(self.sdk, target_is_directory=True)
        self.sdk_output = str(external / "sdk")
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertFalse(self.removed)
        self.assertIn("refused protected target", output)

    def carrier_alias(self, name, destination):
        carrier = self.xcode("Xcode_carrier_" + name + ".app")
        link = carrier / "link"
        link.symlink_to(destination)
        alias = self.root / (name + " alias")
        alias.symlink_to(link)
        return carrier, alias

    def test_all_protected_paths_keep_intermediate_symlink_carriers(self):
        carriers = []
        for variable in ("HOME", "RUNNER_TEMP", "GITHUB_WORKSPACE", "RUNNER_WORKSPACE", "RUNNER_TOOL_CACHE"):
            carrier, alias = self.carrier_alias(variable, self.env[variable])
            carriers.append(carrier)
            self.env[variable] = str(alias)
        for name, destination in (("path", self.root / "HOME"), ("sdk", self.sdk),
                                  ("python", sys.executable), ("helper", HELPER)):
            carrier, alias = self.carrier_alias(name, destination)
            carriers.append(carrier)
            if name == "path":
                self.env["PATH"] += os.pathsep + str(alias)
            elif name == "sdk":
                self.sdk_output = str(alias)
            elif name == "python":
                python_alias = alias
            else:
                helper_alias = alias
        old = self.xcode("Xcode_unused.app")
        with mock.patch.object(cleanup.sys, "executable", str(python_alias)), \
                mock.patch.object(cleanup, "__file__", str(helper_alias)):
            status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.removed, [old])
        self.assertTrue(all((carrier / "link").exists() for carrier in carriers))
        self.assert_selected_preserved()

    def test_default_and_other_direct_toolchain_links_keep_dependency_bundles(self):
        toolchains = self.developer / "Toolchains"
        default = toolchains / "XcodeDefault.xctoolchain"
        shutil.rmtree(default)
        donors = []
        carriers = []
        for name in ("XcodeDefault", "Additional"):
            donor = self.xcode("Xcode_donor_" + name + ".app")
            target = donor / "Contents/Developer/Toolchains/XcodeDefault.xctoolchain"
            carrier, alias = self.carrier_alias(name, target)
            (toolchains / (name + ".xctoolchain")).symlink_to(alias)
            donors.append(donor)
            carriers.append(carrier)
        old = self.xcode("Xcode_unused.app")
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.removed, [old])
        self.assertTrue(all(donor.is_dir() for donor in donors + carriers))
        self.assertTrue(default.is_dir())
        self.assertEqual(self.xcrun_calls, 3)

    def test_symlinked_toolchains_directory_keeps_carrier_and_destination(self):
        toolchains = self.developer / "Toolchains"
        shutil.rmtree(toolchains)
        donor = self.xcode("Xcode_donor.app")
        carrier, alias = self.carrier_alias("Toolchains", donor / "Contents/Developer/Toolchains")
        toolchains.symlink_to(alias)
        old = self.xcode("Xcode_unused.app")
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.removed, [old])
        self.assertTrue(donor.is_dir())
        self.assertTrue(carrier.is_dir())

    def test_xcrun_binaries_keep_intermediate_carriers_and_donor_xcodes(self):
        protected = []
        for tool in ("clang", "ld"):
            donor = self.xcode("Xcode_donor_" + tool + ".app")
            target = donor / "Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin" / tool
            carrier, alias = self.carrier_alias(tool, target)
            protected.extend((donor, carrier))
            self.tool_outputs[tool] = str(alias)
        old = self.xcode("Xcode_unused.app")
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.removed, [old])
        self.assertTrue(all(path.is_dir() for path in protected))
        self.assertEqual(self.xcrun_calls, 3)
        self.assertIn("preserve clang=", output)
        self.assertIn("preserve ld=", output)

    def test_invalid_xcrun_results_fail_before_cleanup(self):
        nonexecutable = self.root / "not executable"
        nonexecutable.write_text("tiny")
        for tool in ("clang", "ld"):
            for value in ("", "relative", str(self.root / "missing"), str(self.root), str(nonexecutable)):
                with self.subTest(tool=tool, value=value), mock.patch.dict(self.tool_outputs, {tool: value}):
                    status, output = self.run_cleanup()
                    self.assertEqual(status, 1, output)
                    self.assertIn(f"xcrun {tool}", output)
                    self.assertFalse(self.commands)
                    self.assertFalse(self.removed)

    def test_protected_symlink_cycles_fail_bounded_before_cleanup(self):
        first, second = self.root / "cycle-a", self.root / "cycle-b"
        first.symlink_to(second)
        second.symlink_to(first)
        for name in ("DEVELOPER_DIR", "HOME", "RUNNER_TEMP", "GITHUB_WORKSPACE",
                     "RUNNER_WORKSPACE", "RUNNER_TOOL_CACHE", "PATH", "sdk", "python", "clang", "ld"):
            with self.subTest(path=name), contextlib.ExitStack() as stack:
                if name == "sdk":
                    stack.enter_context(mock.patch.object(self, "sdk_output", str(first)))
                elif name == "python":
                    stack.enter_context(mock.patch.object(cleanup.sys, "executable", str(first)))
                elif name in ("clang", "ld"):
                    stack.enter_context(mock.patch.dict(self.tool_outputs, {name: str(first)}))
                else:
                    stack.enter_context(mock.patch.dict(self.env, {name: str(first)}))
                status, output = self.run_cleanup()
                self.assertEqual(status, 1, output)
                self.assertIn("symlink cycle or too many links", output)
                self.assertFalse(self.commands)
        (self.developer / "Toolchains/Cyclic.xctoolchain").symlink_to(first)
        status, output = self.run_cleanup()
        self.assertEqual(status, 1, output)
        self.assertIn("symlink cycle or too many links", output)
        self.assertFalse(self.commands)

    def test_chain_preserves_lookup_before_dotdot_and_allows_repeated_links(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "back").symlink_to(self.root)
        carrier, alias = self.carrier_alias("dotdot", nested)
        paths = cleanup.path_chain(alias / "../HOME")
        self.assertIn(carrier / "link", paths)
        self.assertEqual(paths[-1], self.root / "HOME")
        self.assertEqual(cleanup.path_chain(nested / "back/nested/back/HOME")[-1], self.root / "HOME")

    def test_mountpoint_is_not_a_deletion_target(self):
        old = self.xcode("Xcode_mount.app")
        with mock.patch.object(Path, "is_mount", return_value=True):
            status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertFalse(self.removed)
        self.assertTrue(old.exists())

    def test_unreached_cleanup_target_defers_to_actual_space_checks(self):
        old = self.xcode("Xcode_16.app")
        self.gain = 36
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.free, 78)
        self.assertEqual(self.removed, [old])
        self.assertIn("cleanup target not reached", output)
        self.assertIn("actual archive/chunk space checks", output)
        self.assertIn("free_bytes=78", output)
        self.assertNotIn("::error::", output)
        self.assert_selected_preserved()

    def test_no_reclaimable_space_is_reported_and_deletion_errors_still_fail(self):
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertIn("cleanup target not reached", output)
        self.assertIn("required restoration still fails on insufficient space", output)
        self.assertFalse(self.removed)
        self.fail_remove = True
        self.xcode("Xcode_16.app")
        status, output = self.run_cleanup()
        self.assertEqual(status, 1, output)
        self.assertIn("after:", output)
        self.assertIn("::error::macOS disk cleanup failed", output)
        self.assertFalse(self.removed)
        self.assert_selected_preserved()

    def test_guards_do_not_probe_or_delete_on_local_linux_or_self_hosted(self):
        for variable, value in (("CI", "false"), ("GITHUB_ACTIONS", "false"),
                                ("RUNNER_OS", "Linux"), ("RUNNER_ENVIRONMENT", "self-hosted"),
                                ("RUNNER_ENVIRONMENT", "")):
            with self.subTest(variable=variable, value=value), mock.patch.dict(self.env, {variable: value}):
                status, output = self.run_cleanup()
                self.assertEqual(status, 0, output)
                self.assertIn("skipped:", output)
                self.assertEqual(self.xcrun_calls, 0)
                self.assertFalse(self.commands)
        self.system = "Linux"
        status, output = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertFalse(self.commands)
        self.assertEqual(self.xcrun_calls, 0)

    def test_invalid_selection_or_environment_fails_before_deletion(self):
        for variable, value in (("DEVELOPER_DIR", ""), ("DEVELOPER_DIR", "relative"),
                                ("DEVELOPER_DIR", str(self.root)), ("RUNNER_TEMP", str(self.root / "absent")),
                                ("GITHUB_WORKSPACE", "relative")):
            with self.subTest(variable=variable, value=value), mock.patch.dict(self.env, {variable: value}):
                status, output = self.run_cleanup()
                self.assertEqual(status, 1, output)
                self.assertFalse(self.commands)
        self.sdk_output = ""
        status, output = self.run_cleanup()
        self.assertEqual(status, 1, output)
        self.assertFalse(self.commands)

    def test_outside_candidate_and_traversal_rejected(self):
        outside = self.xcode("Xcode_16.app", self.root)
        for path in (outside, self.apps / "../Xcode_16.app", self.apps, Path("/")):
            with self.subTest(path=path):
                self.assertFalse(cleanup.safe_target(path, self.apps, "Xcode*.app", "xcode", [self.selected], []))
        self.assertTrue(outside.exists())


class MacOSDiskWorkflowTest(unittest.TestCase):
    def test_cleanup_precedes_all_downloads_and_restores_on_every_stage(self):
        import yaml
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        self.assertEqual(len(jobs), 9)
        for stage in range(1, 9):
            job = jobs[f"posix-{stage}"]
            with self.subTest(stage=stage):
                steps = job["steps"]
                names = [step.get("name", "") for step in steps]
                cleanup_index = names.index("Free macOS disk space")
                self.assertEqual(names.count("Free macOS disk space"), 1)
                self.assertEqual(names[cleanup_index - 1], "Select compatible Xcode")
                self.assertLess(names.index("Record runner resources"), cleanup_index)
                self.assertLess(cleanup_index, names.index("Inspect macOS toolchain"))
                step = steps[cleanup_index]
                self.assertEqual(step["if"], "runner.os == 'macOS'")
                self.assertIn("set -euo pipefail", step["run"])
                self.assertIn("python3 build/macos/free-disk-space.py", step["run"])
                self.assertIn('tee "${RUNNER_TEMP}/chromix-logs/disk-cleanup.log"', step["run"])
                for name in ("Restore pinned source downloads", f"Run stage {stage}"):
                    self.assertLess(cleanup_index, names.index(name))
                if stage > 1:
                    download_index = names.index("Download tree from previous stage")
                    self.assertLess(cleanup_index, download_index)
                    self.assertEqual(steps[download_index]["if"], "success()")
                diagnostics = steps[names.index("Upload build diagnostics")]
                self.assertEqual(diagnostics["if"], "always()")
                self.assertIn("${{ runner.temp }}/chromix-logs/", diagnostics["with"]["path"])

    def test_generated_workflow_is_deterministic(self):
        with tempfile.TemporaryDirectory(prefix="mac workflow fixture ") as directory:
            generated = Path(directory) / WORKFLOW.relative_to(REPO)
            generated.parent.mkdir(parents=True)
            for _ in range(2):
                subprocess.run([sys.executable, str(REPO / "tools/gen_posix_workflow.py")],
                               cwd=directory, check=True, capture_output=True, timeout=10)
                self.assertEqual(generated.read_bytes(), WORKFLOW.read_bytes())

    @unittest.skipUnless(BASH32.is_file(), "locally built Bash 3.2 required")
    def test_workflow_cleanup_pipeline_runs_under_bash32_and_preserves_failure(self):
        import yaml
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["posix-1"]["steps"]
        script = next(step["run"] for step in steps if step.get("name") == "Free macOS disk space")
        with tempfile.TemporaryDirectory(prefix="disk pipeline fixture ") as directory:
            root = Path(directory)
            helper = root / "build/macos/free-disk-space.py"
            helper.parent.mkdir(parents=True)
            (root / "chromix-logs").mkdir()
            helper.write_text('print("before: fixture\\nafter: fixture", flush=True)\nraise SystemExit(7)\n')
            result = subprocess.run([str(BASH32), "-c", script], cwd=root,
                                    env={**os.environ, "RUNNER_TEMP": str(root)},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertEqual((root / "chromix-logs/disk-cleanup.log").read_text(),
                             "before: fixture\nafter: fixture\n")


if __name__ == "__main__":
    unittest.main()
