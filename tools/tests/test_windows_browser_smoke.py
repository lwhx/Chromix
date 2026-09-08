"""Exercise the Windows smoke helper with mocked process startup on local PowerShell."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
STAGE = REPO / "build/windows/ci-stage.ps1"
LAUNCHER = r"C:\smoke extracted\chromix\chromix.cmd"
WORKING_DIRECTORY = r"C:\smoke extracted\chromix"


class WindowsBrowserSmokeTest(unittest.TestCase):
    def setUp(self):
        self.pwsh = shutil.which("pwsh") or "/opt/pwsh/pwsh"
        if not Path(self.pwsh).is_file():
            self.skipTest("PowerShell is unavailable")
        temp = tempfile.TemporaryDirectory(prefix="windows browser smoke ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        source = STAGE.read_text()
        start = source.index("function Invoke-BoundedBrowser {")
        end = source.index("function Verify-FinalBundle {", start)
        self.script = self.root / "smoke.ps1"
        self.script.write_text(r'''
$ErrorActionPreference = "Stop"
$script:events = [Collections.Generic.List[string]]::new()
$script:waited = $false
$script:handleRead = $false
$script:ready = $env:TEST_IMMEDIATE -eq "1"
function Start-Process {
  param($FilePath, [string[]]$ArgumentList, $WorkingDirectory, [switch]$PassThru,
        $WindowStyle, $RedirectStandardOutput, $RedirectStandardError)
  if ($FilePath -eq (Join-Path $env:SystemRoot "System32/taskkill.exe")) {
    $script:events.Add("tree-start:" + ($ArgumentList -join " "))
    if ($env:TEST_TREE_START_FAILURE -eq "1") { throw "mock taskkill startup failed" }
    $killer = [pscustomobject]@{ Id = 4343 }
    $killer | Add-Member ScriptProperty Handle { return [IntPtr]43 }
    $killer | Add-Member ScriptMethod WaitForExit {
      if ($args.Count -ne 1) { throw "unbounded taskkill wait" }
      $script:events.Add("tree-wait:" + $args[0])
      if ($env:TEST_TREE_HANG -eq "1") { return $false }
      if ($env:TEST_TREE_EXIT -eq "0" -and $env:TEST_REFUSE_EXIT -ne "1") { $script:ready = $true }
      return $true
    }
    $killer | Add-Member ScriptProperty ExitCode {
      $script:events.Add("tree-exit-code")
      return [int]$env:TEST_TREE_EXIT
    }
    $killer | Add-Member ScriptMethod Kill {
      $script:events.Add("tree-kill")
      if ($env:TEST_TREE_KILL_FAILURE -eq "1") { throw "mock taskkill termination failed" }
    }
    $killer | Add-Member ScriptMethod Dispose { $script:events.Add("tree-dispose") }
    return $killer
  }
  $script:events.Add("start")
  @{
    file = $FilePath; arguments = @($ArgumentList); cwd = $WorkingDirectory
    pass_thru = [bool]$PassThru; window_style = $WindowStyle
    stdout = $RedirectStandardOutput; stderr = $RedirectStandardError
  } | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $env:TEST_ROOT "start.json")
  $script:stdout = $RedirectStandardOutput
  $script:stderr = $RedirectStandardError
  Set-Content -LiteralPath $script:stdout -Value "partial" -NoNewline
  Set-Content -LiteralPath $script:stderr -Value "partial" -NoNewline
  if ($env:TEST_START_FAILURE -eq "1") { throw "mock process startup failed" }
  $process = [pscustomobject]@{ Id = 4242 }
  $process | Add-Member ScriptProperty Handle {
    $script:events.Add("handle")
    $script:handleRead = $true
    return [IntPtr]42
  }
  $process | Add-Member ScriptProperty HasExited {
    $script:events.Add("poll")
    return $script:ready
  }
  $process | Add-Member ScriptMethod WaitForExit {
    if ($args.Count -eq 1) {
      $script:events.Add("wait:" + $args[0])
      return $script:ready
    }
    if (-not $script:ready) { throw "unbounded wait on live browser" }
    $script:events.Add("wait")
    $script:waited = $true
    Set-Content -LiteralPath $script:stdout -Value $env:TEST_STDOUT -NoNewline
    Set-Content -LiteralPath $script:stderr -Value $env:TEST_STDERR -NoNewline
  }
  $process | Add-Member ScriptMethod Kill {
    $script:events.Add("browser-kill")
    if ($env:TEST_FALLBACK_FAILURE -eq "1") { throw "mock browser termination failed" }
    if ($env:TEST_REFUSE_EXIT -ne "1") { $script:ready = $true }
  }
  $process | Add-Member ScriptProperty ExitCode {
    $script:events.Add("exit-code")
    if (-not $script:waited -or -not $script:handleRead -or $env:TEST_EXIT -eq "null") { return $null }
    return [int]$env:TEST_EXIT
  }
  $process | Add-Member ScriptMethod Dispose { $script:events.Add("dispose") }
  return $process
}
function Start-Sleep {
  param($Milliseconds)
  if ($Milliseconds -ne 250) { throw "unexpected polling interval" }
  $script:events.Add("sleep")
  $script:ready = $true
}
function taskkill.exe {
  throw "unbounded synchronous taskkill invocation"
}
''' + source[start:end] + r'''
try {
  $browserArguments = @(ConvertFrom-Json $env:TEST_ARGUMENTS)
  $output = Invoke-BoundedBrowser -Launcher $env:TEST_LAUNCHER -Arguments $browserArguments `
    -WorkingDirectory $env:TEST_CWD -TimeoutSec ([int]$env:TEST_TIMEOUT)
  Write-Host ("RESULT:" + (ConvertTo-Json -Compress -InputObject $output))
} finally {
  ConvertTo-Json -Compress -InputObject @($script:events) | Set-Content -LiteralPath (Join-Path $env:TEST_ROOT "events.json")
}
''')

    def run_smoke(self, arguments=None, **env):
        return subprocess.run(
            [self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.script)],
            env={**os.environ, "TEMP": str(self.logs), "TEST_ROOT": str(self.root),
                 "COMSPEC": r"C:\Windows\System32\cmd.exe", "TEST_LAUNCHER": LAUNCHER,
                 "TEST_CWD": WORKING_DIRECTORY, "TEST_ARGUMENTS": json.dumps(arguments or ["--version"]),
                 "TEST_TIMEOUT": "60", "TEST_EXIT": "0", "TEST_STDOUT": "Chromium 152.0.7977.82\n",
                 "TEST_STDERR": "", "TEST_START_FAILURE": "0", "TEST_IMMEDIATE": "0",
                 "SystemRoot": str(self.root / "Windows"), "TEST_TREE_START_FAILURE": "0",
                 "TEST_TREE_HANG": "0", "TEST_TREE_EXIT": "0", "TEST_TREE_KILL_FAILURE": "0",
                 "TEST_REFUSE_EXIT": "0", "TEST_FALLBACK_FAILURE": "0", **env},
            capture_output=True, text=True, timeout=15,
        )

    def startup(self):
        return json.loads((self.root / "start.json").read_text())

    def events(self):
        return json.loads((self.root / "events.json").read_text())

    def assert_clean(self):
        self.assertEqual(list(self.logs.iterdir()), [])

    def test_version_uses_cmd_and_extracted_launcher_with_one_quoted_command_line(self):
        result = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        started = self.startup()
        self.assertEqual(started["file"], r"C:\Windows\System32\cmd.exe")
        self.assertEqual(started["arguments"], [f'/d /v:off /s /c ""{LAUNCHER}" "--version""'])
        self.assertEqual(started["cwd"], WORKING_DIRECTORY)
        self.assertTrue(started["pass_thru"])
        self.assertEqual(started["window_style"], "Hidden")
        self.assertNotEqual(started["stdout"], started["stderr"])
        self.assertEqual(self.events(), ["start", "handle", "poll", "sleep", "poll", "wait", "exit-code", "dispose"])
        output = next(line.removeprefix("RESULT:") for line in result.stdout.splitlines() if line.startswith("RESULT:"))
        self.assertEqual(json.loads(output), "Chromium 152.0.7977.82\n")
        self.assert_clean()

    def test_dom_metacharacters_and_profile_spaces_stay_inside_argument_quotes(self):
        arguments = ["--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                     r"--user-data-dir=C:\smoke extracted\profile", "--dump-dom",
                     "data:text/html,<p>chromix-smoke-ok</p>"]
        result = self.run_smoke(arguments, TEST_STDOUT="<p>chromix-smoke-ok</p>")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        command = self.startup()["arguments"][0]
        self.assertEqual(command, '/d /v:off /s /c "' + ' '.join(f'"{value}"' for value in [LAUNCHER, *arguments]) + '"')
        self.assertIn('"data:text/html,<p>chromix-smoke-ok</p>"', command)
        self.assertNotIn("chrome.exe", command)
        self.assert_clean()

    def test_quoted_shell_metacharacters_and_trailing_backslashes(self):
        arguments = ["--user-data-dir=C:\\profile & (test)!\\", "data:text/html,<p>a&b|c^d!</p>"]
        result = self.run_smoke(arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        command = self.startup()["arguments"][0]
        self.assertIn('"--user-data-dir=C:\\profile & (test)!\\\\"', command)
        self.assertIn('"data:text/html,<p>a&b|c^d!</p>"', command)
        self.assert_clean()

    def test_unsupported_expansion_or_quote_characters_fail_before_start(self):
        for value in ('%TEMP%', 'embedded"quote', "line\rbreak", "line\nbreak", "nul\0byte"):
            with self.subTest(value=value):
                result = self.run_smoke([value])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsupported shell characters", result.stderr)
                self.assertEqual(self.events(), [])
                self.assertFalse((self.root / "start.json").exists())
                self.assert_clean()
        result = self.run_smoke(TEST_LAUNCHER=r"C:\%TEMP%\chromix.cmd")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported shell characters", result.stderr)
        self.assertEqual(self.events(), [])
        self.assert_clean()

    def test_batch_failure_and_missing_exit_code_cannot_report_success(self):
        for code, message in (("7", "failed with exit 7"), ("-1", "failed with exit -1"),
                              ("null", "exit code is unavailable")):
            with self.subTest(code=code):
                result = self.run_smoke(TEST_EXIT=code, TEST_STDOUT="browser output", TEST_STDERR="browser error")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertIn("browser output", result.stdout)
                self.assertIn("browser error", result.stdout)
                self.assertNotIn("RESULT:", result.stdout)
                self.assertLess(self.events().index("wait"), self.events().index("exit-code"))
                self.assertEqual(self.events()[-1], "dispose")
                self.assert_clean()

    def test_timeout_tree_kill_uses_bounded_waits_and_never_reads_success_status(self):
        result = self.run_smoke(TEST_TIMEOUT="-1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out after -1 seconds", result.stderr)
        self.assertEqual(self.events(), ["start", "handle", "poll", "tree-start:/PID 4242 /T /F",
                                         "tree-wait:2000", "tree-exit-code", "tree-dispose", "poll", "wait:2000", "dispose"])
        self.assertNotIn("RESULT:", result.stdout)
        self.assert_clean()

    def test_failed_tree_kill_falls_back_without_unbounded_wait(self):
        for env in ({"TEST_TREE_START_FAILURE": "1"}, {"TEST_TREE_EXIT": "5"}):
            with self.subTest(env=env):
                result = self.run_smoke(TEST_TIMEOUT="-1", **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("timed out after -1 seconds", result.stderr)
                self.assertIn("browser smoke tree cleanup failed", result.stdout)
                self.assertIn("browser-kill", self.events())
                self.assertIn("wait:2000", self.events())
                self.assertNotIn("wait", self.events())
                self.assertEqual(self.events()[-1], "dispose")
                self.assertNotIn("RESULT:", result.stdout)
                self.assert_clean()

    def test_failed_or_ineffective_tree_kill_and_browser_refusing_exit_return_bounded(self):
        for env in ({"TEST_TREE_START_FAILURE": "1"}, {"TEST_TREE_EXIT": "5"}, {}):
            for fallback_failure in ("0", "1"):
                with self.subTest(env=env, fallback_failure=fallback_failure):
                    result = self.run_smoke(TEST_TIMEOUT="-1", TEST_REFUSE_EXIT="1",
                                            TEST_FALLBACK_FAILURE=fallback_failure, **env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("timed out after -1 seconds", result.stderr)
                    self.assertIn("still running after bounded cleanup", result.stdout)
                    self.assertIn("browser-kill", self.events())
                    self.assertIn("wait:2000", self.events())
                    self.assertNotIn("wait", self.events())
                    self.assertNotIn("exit-code", self.events())
                    self.assertEqual(self.events()[-1], "dispose")
                    self.assert_clean()

    def test_hung_taskkill_is_bounded_even_when_its_termination_fails(self):
        for kill_failure in ("0", "1"):
            with self.subTest(kill_failure=kill_failure):
                result = self.run_smoke(TEST_TIMEOUT="-1", TEST_TREE_HANG="1", TEST_REFUSE_EXIT="1",
                                        TEST_TREE_KILL_FAILURE=kill_failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("timed out after -1 seconds", result.stderr)
                self.assertIn("tree-kill", self.events())
                self.assertIn("tree-dispose", self.events())
                self.assertIn("browser-kill", self.events())
                self.assertEqual(self.events().count("tree-wait:2000"), 1)
                self.assertEqual(self.events().count("wait:2000"), 1)
                self.assertNotIn("wait", self.events())
                self.assertEqual(self.events()[-1], "dispose")
                self.assert_clean()

    def test_packaged_launcher_waits_for_browser_instead_of_detaching(self):
        package = (REPO / "build/windows/package-win.ps1").read_text()
        launcher = package.split("@'\n", 1)[1].split("\n'@", 1)[0]
        self.assertEqual(launcher.splitlines(), ["@echo off", '"%~dp0chrome.exe" %*'])

    def test_immediate_exit_still_waits_for_redirected_output_and_retains_status(self):
        result = self.run_smoke(TEST_IMMEDIATE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events(), ["start", "handle", "poll", "wait", "exit-code", "dispose"])
        self.assertIn("Chromium 152.0.7977.82", result.stdout)
        self.assertNotIn("partial", result.stdout)
        self.assert_clean()

    def test_start_failure_cleans_partial_redirects(self):
        result = self.run_smoke(TEST_START_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mock process startup failed", result.stderr)
        self.assertEqual(self.events(), ["start"])
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
