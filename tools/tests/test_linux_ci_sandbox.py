"""Exercise CI AppArmor preparation with fixture kernel files and fake sudo/parser."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "build/linux/prepare-ci-sandbox.sh"
BASH32 = Path.home() / ".local/bash-3.2-for-ci/bash"
KERNEL_PATHS = {
    "clone": "/proc/sys/kernel/unprivileged_userns_clone",
    "maximum": "/proc/sys/user/max_user_namespaces",
    "restriction": "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
    "enabled": "/sys/module/apparmor/parameters/enabled",
}


class LinuxCISandboxTest(unittest.TestCase):
    BASH = shutil.which("bash")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chromix ci sandbox ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        self.script = self.root / "prepare-ci-sandbox.sh"
        self.settings = {}
        source = SCRIPT.read_text()
        for name, production_path in KERNEL_PATHS.items():
            path = self.root / "kernel" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            self.settings[name] = path
            self.assertEqual(source.count(production_path), 1)
            source = source.replace(production_path, shlex.quote(str(path)))
        # Redirect a temporary copy, not the production script or its environment.
        self.assertNotRegex(source, r"/(?:proc|sys)/")
        self.script.write_text(source)
        self.set_settings(clone="1\n", maximum="65536\n", restriction="1\n", enabled="Y\n")
        for name in ("cat", "readlink", "sha256sum"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable, name)
            (self.bin / name).symlink_to(executable)
        self.make_stub("sudo", '''\
import json, os, sys
from pathlib import Path
with Path(os.environ["COMMAND_LOG"]).open("a") as stream:
    stream.write(json.dumps({"command": "sudo", "args": sys.argv[1:]}) + "\\n")
if os.environ.get("FAIL_SUDO"):
    raise SystemExit(1)
parser = Path(__file__).with_name("apparmor_parser")
assert sys.argv[1:] == ["-n", str(parser), "--replace", "--skip-cache"], sys.argv
assert parser.is_file()
os.execv(str(parser), [str(parser)] + sys.argv[3:])
''')
        self.make_stub("apparmor_parser", '''\
import json, os, sys
from pathlib import Path
with Path(os.environ["COMMAND_LOG"]).open("a") as stream:
    stream.write(json.dumps({"command": "apparmor_parser", "args": sys.argv[1:],
                             "profile": sys.stdin.read()}) + "\\n")
raise SystemExit(1 if os.environ.get("FAIL_PARSER") else 0)
''')
        self.browser = self.make_browser(self.root / "extracted build+1/chromix/chrome")
        self.sandbox = self.make_browser(self.browser.with_name("chrome-sandbox"))
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root),
            "LC_ALL": "C",
            "GITHUB_ACTIONS": "true",
            "COMMAND_LOG": str(self.log),
        }

    def make_stub(self, name, source):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)

    def make_browser(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("browser fixture; must never be executed\n")
        path.chmod(0o755)
        return path

    def set_settings(self, **settings):
        for name, value in settings.items():
            path = self.settings[name]
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(value)

    def run_helper(self, *args, env=None):
        self.log.unlink(missing_ok=True)
        before = {name: path.read_bytes() for name, path in self.settings.items() if path.is_file()}
        result = subprocess.run(
            [str(self.BASH), str(self.script), *map(str, args)],
            cwd=self.root, env=self.env if env is None else env,
            capture_output=True, text=True, timeout=10)
        commands = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        after = {name: path.read_bytes() for name, path in self.settings.items() if path.is_file()}
        self.assertEqual(before, after, "kernel settings must remain unchanged")
        self.assertEqual(stat.S_IMODE(self.sandbox.stat().st_mode), 0o755)
        return result, commands

    def assert_no_change(self, result, commands, *, error=None):
        self.assertEqual(commands, [])
        if error is None:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("no profile needed", result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(error, result.stderr)
        self.assertNotIn("Loaded CI", result.stdout)

    def assert_loaded(self, result, commands, browser=None):
        browser = (browser or self.browser).resolve()
        digest = hashlib.sha256(os.fsencode(browser)).hexdigest()
        name = f"chromix-ci-userns-{digest}"
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(commands, [
            {"command": "sudo", "args": ["-n", str(self.bin / "apparmor_parser"),
                                          "--replace", "--skip-cache"]},
            {"command": "apparmor_parser", "args": ["--replace", "--skip-cache"],
             "profile": f'profile "{name}" "{browser}" flags=(unconfined) {{\n  userns,\n}}\n'},
        ])
        self.assertIn(name, result.stdout)
        self.assertIn(str(browser), result.stdout)
        self.assertIn("native browser smoke test", result.stdout)
        self.assertEqual(stat.S_IMODE(browser.stat().st_mode), 0o755)

    def test_loads_only_the_actual_browser_with_apparmor4_syntax(self):
        self.assert_loaded(*self.run_helper(self.browser))

    def test_profile_name_is_repeatable_and_path_bound(self):
        first, commands = self.run_helper(self.browser)
        self.assert_loaded(first, commands)
        repeated, repeated_commands = self.run_helper(self.browser)
        self.assert_loaded(repeated, repeated_commands)
        self.assertEqual(commands, repeated_commands)
        other = self.make_browser(self.root / "other/chrome")
        result, other_commands = self.run_helper(other)
        self.assert_loaded(result, other_commands, other)
        self.assertNotEqual(commands[1]["profile"].split('"')[1],
                            other_commands[1]["profile"].split('"')[1])

    def test_resolves_relative_paths_and_symlinks_to_the_executable(self):
        alias = self.root / "chrome-link"
        alias.symlink_to(self.browser)
        for path in (self.browser.relative_to(self.root), alias, alias.relative_to(self.root)):
            with self.subTest(path=path):
                self.assert_loaded(*self.run_helper(path))

    def test_requires_exactly_one_argument(self):
        for args in ((), (self.browser, "extra")):
            with self.subTest(args=args):
                result, commands = self.run_helper(*args)
                self.assert_no_change(result, commands, error="usage: prepare-ci-sandbox.sh")
                self.assertEqual(result.returncode, 2)

    def test_absent_or_disabled_restriction_needs_no_tools_or_ci_permission(self):
        (self.bin / "apparmor_parser").unlink()
        (self.bin / "sudo").unlink()
        (self.bin / "readlink").unlink()
        (self.bin / "sha256sum").unlink()
        for state in (None, "0\n"):
            with self.subTest(state=state):
                self.set_settings(restriction=state, enabled=None)
                self.assert_no_change(*self.run_helper("missing-browser", env={**self.env, "GITHUB_ACTIONS": "false"}))

    def test_disabled_apparmor_needs_no_profile(self):
        self.set_settings(enabled="N\n")
        self.assert_no_change(*self.run_helper(self.browser, env={**self.env, "GITHUB_ACTIONS": "false"}))

    def test_missing_optional_userns_settings_are_supported(self):
        self.set_settings(clone=None, maximum=None)
        self.assert_loaded(*self.run_helper(self.browser))

    def test_disabled_userns_fails_without_changing_global_settings(self):
        for name, diagnostic in (("clone", "kernel.unprivileged_userns_clone=0"),
                                 ("maximum", "user.max_user_namespaces=0")):
            for restriction in ("1\n", "0\n", None):
                with self.subTest(name=name, restriction=restriction):
                    self.set_settings(clone="1\n", maximum="65536\n", restriction=restriction)
                    self.set_settings(**{name: "0\n"})
                    self.assert_no_change(*self.run_helper(self.browser), error=diagnostic)
        self.set_settings(clone="1\n", maximum="000\n")
        self.assert_no_change(*self.run_helper(self.browser), error="user.max_user_namespaces=0")

    def test_malformed_kernel_numbers_fail_closed(self):
        for name in ("clone", "maximum", "restriction"):
            for value in ("", "-1\n", "1 0\n", "1\n0\n", "unexpected\n"):
                with self.subTest(name=name, value=value):
                    self.set_settings(clone="1\n", maximum="65536\n", restriction="1\n")
                    self.set_settings(**{name: value})
                    self.assert_no_change(*self.run_helper(self.browser), error="invalid kernel setting")

    def test_unknown_boolean_kernel_settings_fail_closed(self):
        for name in ("clone", "restriction"):
            with self.subTest(name=name):
                self.set_settings(clone="1\n", restriction="1\n")
                self.set_settings(**{name: "2\n"})
                self.assert_no_change(*self.run_helper(self.browser), error="unexpected kernel.")

    def test_unreadable_kernel_setting_fails_closed(self):
        path = self.settings["restriction"]
        path.unlink()
        path.mkdir()
        self.assert_no_change(*self.run_helper(self.browser), error="cannot read kernel setting")

    def test_broken_kernel_setting_link_is_not_treated_as_absent(self):
        path = self.settings["restriction"]
        path.unlink()
        path.symlink_to(self.root / "missing-setting")
        self.assert_no_change(*self.run_helper(self.browser), error="cannot read kernel setting")

    def test_unknown_or_missing_apparmor_enabled_state_fails_closed(self):
        for value in (None, "", "1\n", "unknown\n"):
            with self.subTest(value=value):
                self.set_settings(enabled=value)
                error = "cannot read AppArmor enabled state" if value is None else "unexpected AppArmor enabled state"
                self.assert_no_change(*self.run_helper(self.browser), error=error)

    def test_active_restriction_requires_exact_github_actions_opt_in(self):
        for value in (None, "", "false", "1", "TRUE", "true "):
            with self.subTest(value=value):
                env = {**self.env, "CI": "true"}
                env.pop("GITHUB_ACTIONS")
                if value is not None:
                    env["GITHUB_ACTIONS"] = value
                self.assert_no_change(*self.run_helper(self.browser, env=env), error="require GITHUB_ACTIONS=true")

    def test_rejects_non_regular_or_non_executable_paths(self):
        plain = self.make_browser(self.root / "plain-file")
        plain.chmod(0o644)
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o755)
        broken = self.root / "broken-link"
        broken.symlink_to(self.root / "absent")
        for path in (self.root, plain, fifo, broken, self.root / "missing"):
            with self.subTest(path=path):
                result, commands = self.run_helper(path)
                self.assert_no_change(result, commands,
                                      error="cannot resolve executable path" if path in (broken, self.root / "missing")
                                      else "not a regular executable")

    def test_rejects_setid_executable_without_changing_its_permissions(self):
        for mode in (0o4755, 0o2755):
            with self.subTest(mode=oct(mode)):
                self.browser.chmod(mode)
                self.assert_no_change(*self.run_helper(self.browser), error="must not be setuid or setgid")
                self.assertEqual(stat.S_IMODE(self.browser.stat().st_mode), mode)

    def test_rejects_unsafe_path_characters_and_profile_injection(self):
        for character in '*?[]{}"\'\\@^!#,$():;=|<>\t\n\r\x01\x1b\x7fé':
            with self.subTest(character=repr(character)):
                path = self.make_browser(self.root / ("unsafe" + character))
                self.assert_no_change(*self.run_helper(path), error="unsafe executable path")
        self.assert_no_change(*self.run_helper(""), error="unsafe executable path")

    def test_rejects_unsafe_symlink_target_including_trailing_newline(self):
        alias = self.root / "safe-link"
        for suffix in ("*", '"', "\n", "\n\n", "@{target}"):
            with self.subTest(suffix=repr(suffix)):
                target = self.make_browser(self.root / ("target" + suffix))
                alias.unlink(missing_ok=True)
                alias.symlink_to(target)
                self.assert_no_change(*self.run_helper(alias), error="unsafe executable path")

    def test_missing_parser_fails_with_dependency_diagnostic(self):
        (self.bin / "apparmor_parser").unlink()
        self.assert_no_change(*self.run_helper(self.browser), error="apparmor_parser is missing")
        result, _ = self.run_helper(self.browser)
        self.assertIn("install apparmor-utils", result.stderr)

    def test_missing_sudo_fails_explicitly(self):
        (self.bin / "sudo").unlink()
        self.assert_no_change(*self.run_helper(self.browser), error="sudo is missing")

    def test_sudo_and_parser_errors_are_fatal(self):
        for failure, expected in (("FAIL_SUDO", ["sudo"]),
                                  ("FAIL_PARSER", ["sudo", "apparmor_parser"])):
            with self.subTest(failure=failure):
                result, commands = self.run_helper(self.browser, env={**self.env, failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("failed to load CI AppArmor userns profile", result.stderr)
                self.assertNotIn("Loaded CI", result.stdout)
                self.assertEqual([command["command"] for command in commands], expected)

    def test_environment_cannot_override_kernel_fixture_state(self):
        self.set_settings(clone="0\n")
        env = {**self.env, "USERNS_CLONE": "1", "MAX_USERNS": "65536",
               "RESTRICT_USERNS": "0", "APPARMOR_ENABLED": "N",
               "APPARMOR_ENABLED_PATH": str(self.root / "missing")}
        self.assert_no_change(*self.run_helper(self.browser, env=env), error="kernel.unprivileged_userns_clone=0")


@unittest.skipUnless(BASH32.is_file(), "locally built Bash 3.2 required")
class LinuxCISandboxBash32Test(LinuxCISandboxTest):
    BASH = BASH32


if __name__ == "__main__":
    unittest.main()
