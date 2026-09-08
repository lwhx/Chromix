import os
import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
STAGE = REPO / "build" / "windows" / "ci-stage.ps1"
WORKFLOW = REPO / ".github" / "workflows" / "build-win-x64-github.yml"
PREPARE = REPO / "build" / "windows" / "prepare-ungoogled.ps1"


def workflow_job(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n.*?(?=^  [\w-]+:|\Z)", source)
    if match is None:
        raise AssertionError(f"Missing workflow job: {name}")
    return match.group(0)


def workflow_runs(source: str) -> list[str]:
    runs = re.findall(r"(?m)^        run: ([^\n]*(?:\n          [^\n]*)*)", source)
    return [textwrap.dedent(run[2:]) if run.startswith("|\n") else run for run in runs]


class WindowsUpstreamCacheRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stage = STAGE.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.validate = workflow_job(cls.workflow, "validate")
        cls.build_one = workflow_job(cls.workflow, "build-1")
        cls.powershell_runs = workflow_runs(cls.validate)
        for number in range(1, 13):
            cls.powershell_runs.extend(workflow_runs(workflow_job(cls.workflow, f"build-{number}")))
        start = cls.stage.index("if ($StageIndex -eq 1 -and -not $ValidateOnly")
        end = cls.stage.index("\n$UngoogledTooling =", start)
        cls.cache = cls.stage[start:end]
        cls.object_import = cls.stage[
            cls.stage.index("  if ($ImportUpstreamCache) {", end):
            cls.stage.index("  & $gn gen $OutDir", end)
        ]

    def test_gate_one_fresh_build_validation_is_required_with_or_without_cache(self):
        self.assertIn("if: ${{ inputs.resume_run_id == '' }}", self.validate)
        self.assertNotIn("upstream", self.validate.lower())
        self.assertIn("-StageIndex 1 -MaxStages 12 -ValidateOnly", self.validate)
        self.assertIn("needs: validate", self.build_one)
        self.assertIn(
            "if: ${{ always() && inputs.resume_run_id == '' && needs.validate.result == 'success' }}",
            self.build_one,
        )

    def test_gate_two_fetch_is_once_after_cleanup_on_opted_in_fresh_stage_one(self):
        self.assertRegex(
            self.cache,
            r"^if \(\$StageIndex -eq 1 -and -not \$ValidateOnly -and -not \$FromArtifact -and\s+"
            r"\(\$UseUpstreamCache -or \$UpstreamRunId\)\) \{",
        )
        self.assertEqual(self.stage.count("fetch_upstream_cache.py"), 1)
        self.assertEqual(self.stage.count("$fetchRc = Invoke-Tracked"), 1)
        self.assertLess(self.stage.index("\nFree-Disk\n"), self.stage.index(self.cache))
        self.assertIn('$UpstreamCacheDir = "C:\\u"', self.stage)
        self.assertIn(
            '"--platform", "windows", "--arch", "x64", "--destination", $UpstreamCacheDir',
            self.cache,
        )
        self.assertNotIn("actions/download-artifact", self.build_one)
        self.assertNotIn("--repository", self.cache)
        self.assertNotIn("--artifact", self.cache)
        for number in range(2, 13):
            job = workflow_job(self.workflow, f"build-{number}")
            self.assertIn(f"-StageIndex {number} -MaxStages 12 -FromArtifact", job)
            self.assertNotIn("upstream", job.lower())

    def test_gate_three_source_preparation_remains_authoritative_before_imports(self):
        prepare = self.stage.index('& "$PSScriptRoot\\prepare-ungoogled.ps1" -Root $WorkDir')
        fetch = self.stage.index(self.cache)
        toolchain = self.stage.index("--phase toolchain")
        self.assertLess(prepare, fetch)
        self.assertLess(fetch, toolchain)
        self.assertIn('if (-not (Test-Path (Join-Path $Src ".chromix-source-ready"))) {', self.stage)
        self.assertNotIn("$UseUpstreamCache", self.stage[prepare:fetch])
        source = PREPARE.read_text(encoding="utf-8")
        layers = [
            '"utils\\downloads.py"), "unpack"',
            'Invoke-PatchDirectory (Join-Path $Ungoogled "patches")',
            'Invoke-PatchDirectory (Join-Path $Windows "patches")',
            '"utils\\prune_binaries.py"',
            "Invoke-ChromixPatches $resumeChromixPatch",
            'Set-Marker ".chromix-source-ready" $versionKey',
        ]
        offsets = [source.index(layer) for layer in layers]
        self.assertEqual(offsets, sorted(offsets))

    def test_gate_four_toolchain_import_precedes_bootstrap_and_bindgen(self):
        self.assertIn("if ($ImportUpstreamCache) {", self.cache)
        self.assertIn(
            'python (Join-Path $Repo "tools\\import_upstream_cache.py") --phase toolchain',
            self.cache,
        )
        imported = self.stage.index("--phase toolchain")
        self.assertLess(imported, self.stage.index("python tools\\gn\\bootstrap\\bootstrap.py"))
        self.assertLess(imported, self.stage.index("python tools\\rust\\build_bindgen.py --skip-test"))
        self.assertIn('if (-not (Test-Path "third_party\\rust-toolchain\\bin\\bindgen.exe")) {', self.stage)
        self.assertIn('foreach ($binary in @("cargo.exe", "rustc.exe"))', self.stage)

    def test_gate_five_objects_follow_real_substitution_but_precede_gn_gen(self):
        imported = self.stage.index("--phase objects")
        substitution = self.stage.index('python (Join-Path $UngoogledTooling "utils\\domain_substitution.py") apply')
        completed = self.stage.index('Move-Item -LiteralPath $domainProgress -Destination $domainMarker')
        self.assertLess(self.stage.index("tools\\merge_gn_args.py"), substitution)
        self.assertLess(self.stage.index("python tools\\gn\\bootstrap\\bootstrap.py"), substitution)
        self.assertLess(self.stage.index("python tools\\rust\\build_bindgen.py --skip-test"), substitution)
        self.assertLess(substitution, self.stage.index('if ($LASTEXITCODE -ne 0) { throw "domain substitution failed" }'))
        self.assertLess(self.stage.index('throw "domain substitution failed"'), completed)
        self.assertLess(completed, imported)
        self.assertLess(imported, self.stage.index("& $gn gen $OutDir --fail-on-unused-args"))
        self.assertIn("if ($ImportUpstreamCache) {", self.object_import)
        self.assertEqual(self.stage.count("import_upstream_cache.py"), 2)
        for phase in (self.cache, self.object_import):
            self.assertIn("--platform windows --arch x64 --workdir $WorkDir --cache-dir $UpstreamCacheDir", phase)
        self.assertIn('$OutDir = "$Src\\out\\Chromix"', self.stage)
        self.assertIn(
            'python (Join-Path $Repo "tools\\merge_gn_args.py") (Join-Path $OutDir "args.gn") `\n'
            '  (Join-Path $UngoogledTooling "flags.gn") `\n'
            '  (Join-Path $WindowsTooling "flags.windows.gn") `\n'
            '  (Join-Path $Repo "build\\args.windows.gn")',
            self.stage,
        )

    def test_no_source_marker_spoofing_or_direct_donor_tree_import(self):
        self.assertNotIn("UpstreamArtifactPath", self.stage + self.workflow)
        for obsolete in (
            "$upstreamSrc", "$upstreamOut", "$upstreamVersion", "artifacts.zip",
            ".chromix-ungoogled-core", ".chromix-ungoogled-windows", "out\\Default",
        ):
            self.assertNotIn(obsolete, self.stage)
        for code in (self.cache, self.object_import):
            self.assertNotRegex(code, r"Set-Content|Add-Content|WriteAllText|Set-Marker")
            self.assertNotRegex(code, r"Move-Item|Copy-Item|Remove-Item|Expand-Archive|sevenZip")
            self.assertNotIn(".chromix-", code)
            self.assertNotIn("--force", code)
        self.assertNotRegex(
            self.stage,
            r'(?m)^\s*(?:Set-Content|Add-Content|Set-Marker).*\.chromix-',
        )
        writes = re.findall(r'(?m)^\s*(?:Set-Content|Add-Content) -Path (\$domain\w+)', self.stage)
        self.assertEqual(writes, ['$domainProgress'])
        self.assertNotIn('Set-Content -Path $domainMarker', self.stage)
        self.assertEqual(self.stage.count('Move-Item -LiteralPath $domainProgress -Destination $domainMarker'), 1)

    def test_boolean_defaults_disable_cache_unless_run_id_is_explicit(self):
        inputs = re.findall(
            r"(?m)^      use_upstream_cache:\n((?:        [^\n]*\n)+)",
            self.workflow,
        )
        self.assertEqual(len(inputs), 2)
        for fields in inputs:
            self.assertIn("required: false", fields)
            self.assertIn("type: boolean", fields)
            self.assertIn("default: true", fields)
        self.assertIn("[switch]$UseUpstreamCache", self.stage)
        self.assertIn("UseUpstreamCache = ($env:USE_UPSTREAM_CACHE -eq 'true')", self.build_one)
        self.assertIn("($UseUpstreamCache -or $UpstreamRunId)", self.cache)
        self.assertIn('if ($UpstreamRunId) { $fetchArgs += @("--run-id", $UpstreamRunId) }', self.cache)

    def test_user_input_and_optional_token_use_environment_not_shell_interpolation(self):
        runs = self.powershell_runs
        self.assertEqual(len(runs), 25)
        for run in runs:
            self.assertNotIn("${{", run)
        for run in workflow_runs(self.workflow):
            self.assertNotRegex(run, r"\$\{\{\s*inputs\.")
        self.assertIn("UPSTREAM_RUN_ID: ${{ inputs.upstream_run_id }}", self.build_one)
        self.assertIn("USE_UPSTREAM_CACHE: ${{ inputs.use_upstream_cache }}", self.build_one)
        self.assertIn("$stageArgs.UpstreamRunId = $env:UPSTREAM_RUN_ID", self.build_one)
        self.assertIn("& build\\windows\\ci-stage.ps1 @stageArgs", self.build_one)
        self.assertIn(r"[ValidatePattern('\A[0-9]*\z')] [string]$UpstreamRunId", self.stage)
        self.assertIn("GH_TOKEN: ${{ secrets.UPSTREAM_ACTIONS_TOKEN || github.token }}", self.build_one)
        self.assertRegex(self.workflow, r"secrets:\s+UPSTREAM_ACTIONS_TOKEN:\s+required: false")
        self.assertNotIn("GH_TOKEN", "\n".join(runs))
        self.assertNotIn("Invoke-Expression", self.stage + self.workflow)
        self.assertIn('$fetchCommandLine = ($fetchArgs | ForEach-Object { "`"$_`"" }) -join " "', self.cache)
        self.assertIn("-ArgList $fetchCommandLine -Cwd $Repo", self.cache)

    def test_optional_misses_continue_through_both_phases_and_normal_build(self):
        self.assertIn("$ImportUpstreamCache = $false", self.stage)
        self.assertIn("$ImportUpstreamCache = $true", self.cache)
        self.assertRegex(
            self.cache,
            r'if \(\$fetchRc -eq 124\) \{\s+Write-Host "[^"\n]+"\s+'
            r'\} elseif \(\$fetchRc -ne 0\) \{\s+'
            r'throw "upstream cache fetch helper failed \(exit \$fetchRc\)"\s+'
            r'\} else \{\s+\$ImportUpstreamCache = \$true',
        )
        for helper in ("toolchain import", "object import"):
            self.assertIn(
                f'if ($LASTEXITCODE -ne 0) {{ throw "upstream {helper} helper failed (exit $LASTEXITCODE)" }}',
                self.stage,
            )
        for code in (self.cache, self.object_import):
            self.assertNotRegex(code, r"\b(?:return|exit)\s+[0-9]|Write-OutVar finished")
            self.assertNotIn("Test-Path", code)
            self.assertNotIn("ConvertFrom-Json", code)
            self.assertNotIn("--required", code)
        generated = self.stage.index("& $gn gen $OutDir --fail-on-unused-args")
        validation = self.stage.index("if ($ValidateOnly) {")
        compilation = self.stage.index('$rc = Invoke-Tracked -File (Join-Path $Src "third_party\\ninja\\ninja.exe")')
        self.assertLess(generated, validation)
        self.assertLess(validation, compilation)
        self.assertIn('throw "gn gen failed"', self.stage)
        self.assertIn('throw "V8 Torque validation failed (exit $validationRc)"', self.stage)
        self.assertIn('throw "ninja failed (exit $rc)"', self.stage)
        package = self.stage.index('& "$PSScriptRoot\\package-win.ps1"')
        verified = self.stage.index("\n  Verify-FinalBundle\n", package)
        finished = self.stage.index("Write-OutVar finished true", verified)
        self.assertLess(package, verified)
        self.assertLess(verified, finished)

    def test_cache_respects_existing_stage_budget(self):
        self.assertIn("$Deadline = (Get-Date).AddMinutes(300)", self.stage)
        self.assertIn("$PackReserveMin = 40", self.stage)
        self.assertIn("if ((Get-RemainingMin) -lt ($PackReserveMin + 60)) {", self.cache)
        self.assertIn("skipping optional upstream cache: insufficient stage budget", self.cache)
        self.assertIn("-ArgList $fetchCommandLine -Cwd $Repo -TimeoutSec 1200", self.cache)
        self.assertIn("optional upstream cache timed out; continuing with the prepared source", self.cache)
        self.assertNotIn("--timeout", self.cache)
        self.assertNotIn("--retries", self.cache)
        self.assertEqual(self.workflow.count("timeout-minutes: 355"), 12)

    def test_stage_one_uploads_only_cache_reports_even_after_failure(self):
        start = self.build_one.index("      - name: Upload upstream cache diagnostics")
        end = self.build_one.index("      - name: Ensure build tree snapshot", start)
        diagnostics = self.build_one[start:end]
        self.assertIn("if: ${{ always() }}", diagnostics)
        self.assertIn("uses: actions/upload-artifact@v4", diagnostics)
        self.assertIn("if-no-files-found: ignore", diagnostics)
        paths = re.search(r"path: \|\n((?:            [^\n]+\n)+)", diagnostics)
        self.assertIsNotNone(paths)
        self.assertEqual(
            [line.strip() for line in paths.group(1).splitlines()],
            [r"C:\u\result.json", r"C:\c\chromix\upstream-cache-import.json",
             r"C:\c\chromix\upstream-cache-plan.log"],
        )
        self.assertNotIn(r"C:\u\tree", self.workflow)
        self.assertNotIn(r"C:\u\*", self.workflow)
        self.assertEqual(self.workflow.count("Upload upstream cache diagnostics"), 1)
        self.assertEqual(self.workflow.count("- name: Ensure build tree snapshot"), 12)
        self.assertEqual(self.workflow.count("- name: Upload tree part"), 48)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is not installed; parse-only check unavailable")
    def test_powershell_stage_and_workflow_commands_parse_without_execution(self):
        stage_parser = r"""
$tokens = $null
$errors = $null
[Management.Automation.Language.Parser]::ParseFile($env:STAGE_PATH, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count -gt 0) {
  $errors | ForEach-Object { Write-Error $_.Message }
  exit 1
}
"""
        run_parser = r"""
$tokens = $null
$errors = $null
[Management.Automation.Language.Parser]::ParseInput($env:WORKFLOW_RUN, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count -gt 0) {
  $errors | ForEach-Object { Write-Error $_.Message }
  exit 1
}
"""
        checks = [(stage_parser, {"STAGE_PATH": str(STAGE)})]
        checks.extend((run_parser, {"WORKFLOW_RUN": run}) for run in self.powershell_runs)
        for parser, values in checks:
            with self.subTest(values=values):
                result = subprocess.run(
                    [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", parser],
                    env={**os.environ, **values},
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
