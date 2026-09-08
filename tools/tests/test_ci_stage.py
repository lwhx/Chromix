import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CI_STAGE = REPO / "build" / "windows" / "ci-stage.ps1"
WORKFLOW = REPO / ".github" / "workflows" / "build-win-x64-github.yml"
RESTORED_SOURCE_UPDATE = REPO / "build" / "windows" / "update-restored-source.ps1"
PREPARE_UNGOOGLED = REPO / "build" / "windows" / "prepare-ungoogled.ps1"
README = REPO / "README.md"
PYTHON_BINARY = REPO / "sdk" / "python" / "chromix" / "_binary.py"
NODE_BINARY = REPO / "sdk" / "node" / "_binary.js"
NODE_INDEX = REPO / "sdk" / "node" / "index.js"
TIMEZONE_PATCH = (
    REPO
    / "patches"
    / "0019-third_party-blink-renderer-core-timezone-timezone_controller-cc.patch"
)
WEBGL1_PERSONA_PATCH = (
    REPO
    / "patches"
    / "0029-third_party-blink-renderer-modules-webgl-webgl_rendering_context_base-cc.patch"
)
CANVAS2D_BRIDGE_PATCH = (
    REPO
    / "patches"
    / "0076-third_party-blink-renderer-modules-canvas-canvas2d-base_rendering_context_2d-cc.patch"
)


def invoke_tracked_source() -> str:
    script = CI_STAGE.read_text(encoding="utf-8")
    start = script.index("function Invoke-Tracked {")
    end = script.index("function Get-FreeGB", start)
    return script[start:end]


def validate_only_source() -> str:
    script = CI_STAGE.read_text(encoding="utf-8")
    start = script.index("if ($ValidateOnly) {")
    end = script.index("$ninjaBudget =", start)
    return script[start:end]


class InvokeTrackedRegressionTest(unittest.TestCase):
    def test_uses_cmd_wrapper_status_instead_of_process_exit_code(self):
        source = invoke_tracked_source()
        self.assertIn(
            '$wrapperName = "ci-tracked-$PID-$([Guid]::NewGuid().ToString(\'N\'))"',
            source,
        )
        self.assertIn('$wrapper = Join-Path $env:TEMP "$wrapperName.cmd"', source)
        self.assertIn('$status = Join-Path $env:TEMP "$wrapperName.exit"', source)
        self.assertRegex(
            source,
            r'"`"\$cmdFile`" \$cmdArgs",\s+'
            r"'set \"ci_tracked_exit=%ERRORLEVEL%\"',\s+"
            r'">`"\$cmdStatus`" echo %ci_tracked_exit%",\s+'
            r'"exit /b %ci_tracked_exit%"',
        )
        self.assertNotIn("$process.ExitCode", source)

    def test_cleans_old_tracking_files_and_quotes_wrapper_path(self):
        source = invoke_tracked_source()
        self.assertIn(
            "Remove-Item $log, $err, $wrapper, $status -ErrorAction SilentlyContinue",
            source,
        )
        self.assertIn('$File.Replace("%", "%%")', source)
        self.assertIn('$ArgList.Replace("%", "%%")', source)
        self.assertIn('$status.Replace("%", "%%")', source)
        self.assertRegex(
            source,
            r'Start-Process -FilePath \$env:COMSPEC\s+`\n'
            r'\s+-ArgumentList "/d /s /c `"`"\$wrapper`"`""',
        )

    def test_waits_before_strictly_parsing_status_file(self):
        source = invoke_tracked_source()
        self.assertEqual(source.count("$process.WaitForExit()"), 2)
        self.assertRegex(
            source,
            r"\n    \}\n    \$process\.WaitForExit\(\)\n\n"
            r"    \$code = 1\n"
            r"    if \(-not \(Test-Path -LiteralPath \$status -PathType Leaf\)\)",
        )
        self.assertIn("$statusValue -notmatch '^-?\\d+$'", source)
        self.assertIn("[int]::TryParse($statusValue, [ref]$parsedCode)", source)

    def test_missing_or_invalid_status_fails_conservatively(self):
        source = invoke_tracked_source()
        self.assertIn("$code = 1", source)
        self.assertIn(
            'Write-Host "==> tracked process exit status file is missing: '
            '$status; treating as failure"',
            source,
        )
        self.assertIn(
            'Write-Host "==> tracked process exit status is invalid: '
            "'$displayStatus'; treating as failure\"",
            source,
        )
        self.assertIn('Write-Host "==> tracked process exit code: $code"', source)

    def test_timeout_still_returns_124(self):
        source = invoke_tracked_source()
        self.assertRegex(
            source,
            r"(?s)taskkill\.exe /PID \$process\.Id /T /F.*?"
            r"\$process\.WaitForExit\(\).*?return 124",
        )

    def test_failure_output_keeps_long_stdout_tail_and_full_stderr(self):
        source = invoke_tracked_source()
        stdout_tails = re.findall(r"Get-Content \$log -Tail (\d+)", source)
        self.assertTrue(stdout_tails)
        self.assertGreaterEqual(max(map(int, stdout_tails)), 100)
        self.assertIn("Get-Content $err | ForEach-Object", source)
        self.assertNotRegex(source, r"Get-Content \$err -Tail \d+")

    def test_can_emit_complete_stdout_on_failure(self):
        source = invoke_tracked_source()
        self.assertIn("[switch]$FullFailureOutput", source)
        self.assertRegex(
            source,
            r"if \(\$FullFailureOutput\) \{\s+Write-Host "
            r'"==> tracked process stdout \(complete\)"\s+'
            r"Get-Content \$log \| ForEach-Object",
        )


class ValidateOnlyRegressionTest(unittest.TestCase):
    def test_serial_verbose_ninja_preserves_failures_and_full_output(self):
        source = validate_only_source()
        self.assertIn(
            '-ArgList "-C `"$OutDir`" -j 1 -v '
            'gen/v8/torque-generated/bit-field-asserts.cc"',
            source,
        )
        self.assertIn("-FullFailureOutput", source)
        self.assertRegex(
            source,
            r'if \(\$validationRc -ne 0\) \{ throw "V8 Torque validation failed '
            r'\(exit \$validationRc\)" \}',
        )
        self.assertNotIn("Test-Path", source)


class DomainSubstitutionRegressionTest(unittest.TestCase):
    def setUp(self):
        self.stage = CI_STAGE.read_text(encoding="utf-8")
        guard_start = self.stage.index('$domainProgress = Join-Path $Src')
        guard_end = self.stage.index('\n$MigrateRestoredSource =', guard_start)
        self.guard = self.stage[guard_start:guard_end]
        start = self.stage.index('  if (-not (Test-Path $domainMarker)) {')
        end = self.stage.index('  & $gn gen $OutDir', start)
        self.substitution = self.stage[start:end]

    def test_matches_native_windows_substitution_after_tool_setup(self):
        native = (REPO / "build/windows/build.ps1").read_text(encoding="utf-8")
        for argument in (
            'python (Join-Path $UngoogledTooling "utils\\domain_substitution.py") apply',
            '-r (Join-Path $UngoogledTooling "domain_regex.list")',
            '-f (Join-Path $WindowsTooling "domain_substitution.list")',
        ):
            self.assertIn(argument, native)
            self.assertIn(argument, self.substitution)
        self.assertIn('-c $domainCache $Src', self.substitution)
        self.assertIn('domain_substitution_cache.tar.gz', self.substitution)
        start = self.stage.index(self.substitution)
        self.assertLess(self.stage.index('throw "bindgen build failed"'), start)
        self.assertLess(self.stage.index('throw "GN bootstrap failed"'), start)
        self.assertNotIn('--phase objects', self.stage)
        self.assertLess(start, self.stage.index('& $gn gen $OutDir'))
        self.assertNotIn('$ValidateOnly', self.substitution)
        self.assertNotIn('$ImportUpstreamCache', self.substitution)

    def test_interrupted_restore_is_rejected_before_source_migration_or_import(self):
        guard = self.stage.index(self.guard)
        restore = self.stage.index('& $sevenZip x "C:\\restore\\tree.7z.001"')
        self.assertLess(restore, guard)
        self.assertLess(guard, self.stage.index('update-restored-source.ps1', restore))
        self.assertLess(guard, self.stage.index('prepare-ungoogled.ps1', restore))
        self.assertLess(guard, self.stage.index('--phase restore'))
        self.assertIn('if (Test-Path $domainProgress)', self.guard)
        self.assertNotIn('Test-Path $domainMarker', self.guard)
        self.assertNotIn('Remove-Item', self.guard)
        progress = self.substitution.index('Set-Content -Path $domainProgress')
        apply = self.substitution.index('python (Join-Path $UngoogledTooling')
        checked = self.substitution.index('if ($LASTEXITCODE -ne 0)')
        completed = self.substitution.index('Move-Item -LiteralPath $domainProgress -Destination $domainMarker')
        self.assertLess(progress, apply)
        self.assertLess(apply, checked)
        self.assertLess(checked, completed)
        self.assertNotIn('Set-Content -Path $domainMarker', self.stage)

    def fixture(self, fail=False):
        import os
        import shutil
        import sys
        import tempfile

        powershell = shutil.which("pwsh")
        if not powershell:
            self.skipTest("pwsh is unavailable")
        utils = (REPO / ".chromix-build-verify/tooling/ungoogled-chromium/utils")
        if not (utils / "domain_substitution.py").is_file():
            self.skipTest("pinned ungoogled-chromium verification tooling is unavailable")
        temp = tempfile.TemporaryDirectory(prefix="chromix domain substitution ")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        src = root / "src"
        src.mkdir()
        core = root / "tooling/ungoogled-chromium"
        windows = root / "tooling/ungoogled-chromium-windows"
        (core / "utils").mkdir(parents=True)
        windows.mkdir(parents=True)
        for name in ("domain_substitution.py", "_common.py", "_extraction.py"):
            shutil.copy2(utils / name, core / "utils" / name)
        (core / "domain_regex.list").write_text(r"google\.test#blocked.test" + "\n")
        (windows / "domain_substitution.list").write_text(
            "source.cc\n" + ("invalid|entry\n" if fail else ""))
        (src / "source.cc").write_text('const char* url = "https://google.test/path";\n')
        # A core-only entry must not replace the Windows platform file list.
        (core / "domain_substitution.list").write_text("untouched.cc\n")
        (src / "untouched.cc").write_text("google.test\n")
        (root / "gn.ps1").write_text(
            'Add-Content -Path $env:DOMAIN_TEST_CALLS -Value "gn"\n'
            '$global:LASTEXITCODE = 0\n')
        end = self.stage.index('  if ($RestoredUpstream) {',
                               self.stage.index('throw "gn gen failed"'))
        pipeline = self.stage[self.stage.index(self.substitution):end]
        script = root / "fixture.ps1"
        script.write_text(r'''
$ErrorActionPreference = "Stop"
$WorkDir = $env:DOMAIN_TEST_ROOT
$Src = Join-Path $WorkDir "src"
$Repo = $WorkDir
$OutDir = Join-Path $Src "out"
$UngoogledTooling = Join-Path $WorkDir "tooling/ungoogled-chromium"
$WindowsTooling = Join-Path $WorkDir "tooling/ungoogled-chromium-windows"
$Revisions = @{ UngoogledCommit = "fixture-core-commit" }
$RestoredUpstream = $false
$UpstreamCacheDir = Join-Path $WorkDir "cache"
$gn = Join-Path $WorkDir "gn.ps1"
function python {
  Add-Content -Path $env:DOMAIN_TEST_CALLS -Value "substitution"
  & $env:DOMAIN_TEST_PYTHON @args
  $global:LASTEXITCODE = $LASTEXITCODE
}
''' + self.guard + "\n" + pipeline)
        env = {**os.environ, "DOMAIN_TEST_ROOT": str(root),
               "DOMAIN_TEST_PYTHON": sys.executable,
               "DOMAIN_TEST_CALLS": str(root / "calls"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        return root, [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], env

    def run_fixture(self, command, env, imported=True):
        import subprocess

        return subprocess.run(command, env={**env, "DOMAIN_TEST_IMPORT": str(int(imported))},
                              capture_output=True, text=True, timeout=20)

    def test_real_substitution_then_gn_and_idempotent_resume(self):
        import tarfile

        root, command, env = self.fixture()
        src = root / "src"
        original = (src / "source.cc").read_bytes()
        result = self.run_fixture(command, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b"https://blocked.test/path", (src / "source.cc").read_bytes())
        self.assertEqual((src / "untouched.cc").read_text(), "google.test\n")
        self.assertFalse((src / ".chromix-domain-substitution-in-progress").exists())
        self.assertEqual((src / ".chromix-domain-substituted").read_text().strip(),
                         "fixture-core-commit")
        cache = root / "domain_substitution_cache.tar.gz"
        with tarfile.open(cache) as archive:
            self.assertEqual(archive.extractfile("orig/source.cc").read(), original)
            self.assertIn(b"source.cc|", archive.extractfile("cache_index.list").read())
        self.assertEqual((root / "calls").read_text().splitlines(),
                         ["substitution", "gn"])
        modified = (src / "source.cc").stat().st_mtime_ns
        cache_bytes = cache.read_bytes()
        resumed = self.run_fixture(command, env)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual((src / "source.cc").stat().st_mtime_ns, modified)
        self.assertEqual(cache.read_bytes(), cache_bytes)
        self.assertEqual((root / "calls").read_text().splitlines(),
                         ["substitution", "gn", "gn"])

    def test_substitution_is_required_without_upstream_cache(self):
        root, command, env = self.fixture()
        result = self.run_fixture(command, env, imported=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / "calls").read_text().splitlines(), ["substitution", "gn"])
        self.assertTrue((root / "src/.chromix-domain-substituted").is_file())

    def test_partial_failure_is_not_stamped_or_retried_on_restore(self):
        root, command, env = self.fixture(fail=True)
        result = self.run_fixture(command, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("domain substitution failed", result.stderr)
        src = root / "src"
        self.assertIn("blocked.test", (src / "source.cc").read_text())
        self.assertTrue((src / ".chromix-domain-substitution-in-progress").is_file())
        self.assertFalse((src / ".chromix-domain-substituted").exists())
        self.assertEqual((root / "calls").read_text().splitlines(), ["substitution"])
        resumed = self.run_fixture(command, env)
        self.assertNotEqual(resumed.returncode, 0)
        self.assertIn("domain substitution was interrupted", resumed.stderr)
        self.assertEqual((root / "calls").read_text().splitlines(), ["substitution"])
        self.assertFalse((src / ".chromix-domain-substituted").exists())

    def test_interrupted_marker_wins_over_completion_marker_on_restore(self):
        root, command, env = self.fixture()
        for marker in (".chromix-domain-substitution-in-progress", ".chromix-domain-substituted"):
            (root / "src" / marker).touch()
        result = self.run_fixture(command, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("domain substitution was interrupted", result.stderr)
        self.assertFalse((root / "calls").exists())

    def test_existing_cache_without_marker_never_manufactures_completion(self):
        for name in ("domain_substitution_cache.tar.gz", "domain_substitution_cache.tar"):
            with self.subTest(cache=name):
                root, command, env = self.fixture()
                (root / name).write_bytes(b"unproven cache")
                result = self.run_fixture(command, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cache exists without a completion marker", result.stderr)
                self.assertFalse((root / "src/.chromix-domain-substituted").exists())
                self.assertFalse((root / "calls").exists())


class ResumeWorkflowRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")

    def test_resume_skips_predecessors_and_starts_requested_stage(self):
        self.assertIn("resume_run_id:", self.source)
        self.assertIn(
            "if: ${{ inputs.resume_run_id == '' }}",
            self.source,
        )
        for stage in range(2, 13):
            self.assertIn(f"inputs.resume_stage == '{stage}'", self.source)
            self.assertIn(
                f"inputs.resume_run_id == '' || inputs.resume_stage != '{stage}'",
                self.source,
            )
            self.assertIn(
                f"pattern: tree-s{stage - 1}-attempt-",
                self.source,
            )
        self.assertEqual(self.source.count("Download tree from previous run"), 11)

    def test_every_stage_uploads_a_tree_on_success_or_failure(self):
        self.assertEqual(self.source.count("- name: Ensure build tree snapshot"), 12)
        self.assertEqual(
            self.source.count(
                "if: ${{ always() && steps.stage.outputs.upload_parts != 'true' }}"
            ),
            12,
        )
        self.assertEqual(len(re.findall(r"- name: Upload tree part [1-4]\n        if: \$\{\{ always\(\) \}\}", self.source)), 48)
        self.assertEqual(self.source.count("- name: Upload tree part 1"), 12)
        self.assertEqual(self.source.count("- name: Upload tree part 4"), 12)
        self.assertNotIn("if: steps.stage.outputs.upload_parts == 'true'", self.source)
        self.assertEqual(
            self.source.count(
                ". build\\windows\\ci-parts.ps1 -Root C:\\c "
                "-PartsDir C:\\parts -Mode Synced"
            ),
            12,
        )

    def test_upstream_cache_keeps_validation_and_imports_only_stage_one(self):
        self.assertIn("upstream_run_id:", self.source)
        self.assertIn("use_upstream_cache:", self.source)
        self.assertIn("inputs.resume_run_id == '' && needs.validate.result == 'success'", self.source)
        self.assertIn("GH_TOKEN: ${{ secrets.UPSTREAM_ACTIONS_TOKEN || github.token }}", self.source)
        self.assertIn("UPSTREAM_RUN_ID: ${{ inputs.upstream_run_id }}", self.source)
        self.assertEqual(self.source.count("UseUpstreamCache ="), 1)
        self.assertNotIn("-UpstreamArtifactPath", self.source)

    def test_resume_uses_official_cross_run_artifact_download(self):
        self.assertIn("actions: read", self.source)
        self.assertEqual(self.source.count("github-token: ${{ github.token }}"), 11)
        self.assertEqual(self.source.count("run-id: ${{ inputs.resume_run_id }}"), 11)
        self.assertEqual(self.source.count("merge-multiple: true"), 22)
        self.assertIn("resume_tree_stage:", self.source)
        self.assertIn(
            "pattern: tree-s${{ inputs.resume_tree_stage != '' && inputs.resume_tree_stage || '11' }}-attempt-*-part*",
            self.source,
        )
        self.assertNotIn("download-stage-artifacts.ps1", self.source)


class ReleaseChannelRegressionTest(unittest.TestCase):
    def test_sdk_channels_match_documented_verified_releases(self):
        readme = README.read_text(encoding="utf-8")
        python_binary = PYTHON_BINARY.read_text(encoding="utf-8")
        node_binary = NODE_BINARY.read_text(encoding="utf-8")
        node_index = NODE_INDEX.read_text(encoding="utf-8")
        channels = {
            "stable": "v151.0.7922.173",
            "latest": "v152.0.7977.75",
        }
        for channel, tag in channels.items():
            self.assertIn(f'"{channel}": {{"tag": "{tag}"}}', python_binary)
            self.assertIn(f'{channel}: {{ tag: "{tag}" }}', node_binary)
            self.assertIn(f"releases/tag/{tag}", readme)
        self.assertIn('export const CHROMIUM_VERSION = "152";', node_index)


class RestoredSourceUpdateRegressionTest(unittest.TestCase):
    def test_upstream_cache_restores_before_preparation_without_stamping_unproven_layers(self):
        stage = CI_STAGE.read_text(encoding="utf-8")
        self.assertIn('[switch]$UseUpstreamCache', stage)
        self.assertNotIn('$UpstreamArtifactPath', stage)
        self.assertNotIn('Move-Item $upstreamSrc $Src', stage)
        self.assertNotIn('Set-Content -Path (Join-Path $Src ".chromix-ungoogled-core")', stage)
        self.assertNotIn('Set-Content -Path (Join-Path $Src ".chromix-ungoogled-windows")', stage)
        prepare = stage.index('& "$PSScriptRoot\\prepare-ungoogled.ps1"')
        restored = stage.index('--phase restore')
        self.assertLess(restored, prepare)
        self.assertNotIn('import_upstream_cache.py', stage)

    def test_resume_source_update_avoids_powershell_host_automatic_variable(self):
        update_source = RESTORED_SOURCE_UPDATE.read_text(encoding="utf-8")
        self.assertNotRegex(update_source, r"(?im)^\s*\$host\s*=")
        self.assertIn("$hostSource", update_source)

    def test_resume_source_update_terminates_ua_here_string_before_preprocessor_line(self):
        update_source = RESTORED_SOURCE_UPDATE.read_text(encoding="utf-8")
        self.assertIn('$uaInternal += "`n"', update_source)

    def test_resume_updates_stale_media_recorder_source(self):
        stage_source = CI_STAGE.read_text(encoding="utf-8")
        update_source = RESTORED_SOURCE_UPDATE.read_text(encoding="utf-8")
        restore = stage_source.index('& $sevenZip x "C:\\restore\\tree.7z.001"')
        update = stage_source.index('update-restored-source.ps1', restore)
        prepare = stage_source.index('& "$PSScriptRoot\\prepare-ungoogled.ps1"', restore)
        self.assertLess(restore, prepare)
        self.assertLess(prepare, update)
        self.assertIn('$normalizedContent = $content.Replace("`r`n", "`n")', update_source)
        self.assertIn('$normalizedOldText = $OldText.Replace("`r`n", "`n")', update_source)
        self.assertIn('$normalizedNewText = $NewText.Replace("`r`n", "`n")', update_source)
        self.assertIn('$normalizedContent.Contains($normalizedOldText)', update_source)
        self.assertIn('      $path, $normalizedContent.Replace($normalizedOldText, $normalizedNewText))', update_source)
        self.assertIn("type.LowerASCII().Utf8()", update_source)
        self.assertIn("type.ToAsciiLower().Utf8()", update_source)
        self.assertIn("json_file_value_deserializer.h", update_source)
        self.assertIn("json_file_value_serializer.h", update_source)
        self.assertIn("base::Value::Dict", update_source)
        self.assertIn("base::DictValue", update_source)
        self.assertIn("uxr-webgl-renderer", update_source)
        self.assertIn("uxr-webgl-vendor", update_source)
        self.assertIn("WebGLPersonaRenderer", update_source)
        self.assertIn("WebGLPersonaVendor", update_source)
        self.assertIn("base::UxrConfig::GetInstance()", update_source)
        self.assertIn('#include "base/uxr_config.h"', update_source)
        self.assertIn("ClampWebGLPersonaLimit", update_source)
        self.assertIn("ClampWebGL2PersonaLimit", update_source)
        self.assertIn("WebGL 2.0 (OpenGL ES 3.0 Chromium)", update_source)
        self.assertIn("UxrFarbleReadPixels", update_source)
        self.assertIn("UxrJitterMetric", update_source)
        self.assertIn("UxrFontFamilyAllowed", update_source)
        self.assertIn("kWindowsFamilies", update_source)
        self.assertIn('Get("uxr-platform")', update_source)
        self.assertIn("base::SplitString", update_source)
        self.assertIn("#include \"components/ungoogled/farble_seed.h\"", update_source)
        self.assertIn("#include \"components/ungoogled/persona_profile.h\"", update_source)
        self.assertIn("farble_seed.h", update_source)
        self.assertIn("farble_seed.cc", update_source)
        self.assertIn("fingerprint_data.h", update_source)
        self.assertIn("FontCache::GetFontPlatformData", update_source)
        self.assertIn("TextMetrics::Update", update_source)
        current = update_source.rindex('$normalizedContent.Contains($normalizedNewText)')
        stale = update_source.index('$normalizedContent.Contains($normalizedOldText)')
        self.assertGreater(current, stale)
        self.assertIn("Normalize-RestoredSource", update_source)
        normalize_definition = update_source.index("function Normalize-RestoredSource {")
        first_normalize_call = update_source.index("Normalize-RestoredSource `")
        self.assertLess(normalize_definition, first_normalize_call)
        self.assertIn("normalized restored source", update_source)
        self.assertIn("already current or not applicable", update_source)
        self.assertNotIn(
            "resume source migration has unknown state (expected legacy text or current marker)",
            update_source,
        )
        self.assertIn("UxrFontFamilyIsGeneric", update_source)
        self.assertIn("NVIDIA GeForce RTX 3060 Direct3D11", update_source)
        self.assertIn("String(WebGLPersonaRenderer().c_str())", update_source)
        self.assertIn("String(WebGLPersonaVendor().c_str())", update_source)
        self.assertIn("String(ungoogled::CurrentPersona().webgl_vendor)", update_source)
        self.assertIn("String(ungoogled::CurrentPersona().webgl_renderer)", update_source)
        self.assertIn("UxrJitterQuads", update_source)
        self.assertIn('third_party\\blink\\renderer\\core\\dom\\element.cc', update_source)
        self.assertIn('third_party\\blink\\renderer\\modules\\webgpu\\gpu_adapter_info.cc', update_source)
        self.assertIn('third_party\\blink\\renderer\\modules\\webgpu\\gpu_adapter.cc', update_source)
        self.assertIn('third_party\\blink\\renderer\\modules\\BUILD.gn', update_source)
        self.assertIn('third_party\\blink\\renderer\\modules\\webgl\\BUILD.gn', update_source)
        self.assertIn(
            'third_party\\blink\\renderer\\modules\\canvas\\canvas2d\\base_rendering_context_2d.cc',
            update_source,
        )
        self.assertIn('bridge->BridgeEnabledForOrigin(origin->RegistrableDomain().Utf8())) {', update_source)
        self.assertIn('      SkPixmap pixmap = image_data->GetSkPixmap();', update_source)
        self.assertIn('    }\n  }\n\'@', update_source)
        self.assertIn('"//components/ungoogled",', update_source)
        self.assertIn('"//components/ungoogled:ungoogled_switches",', update_source)
        self.assertIn('chromix-renderer-objects-invalidated.txt', update_source)
        self.assertIn("$namespaceMarker", update_source)
        self.assertIn("$content.Insert($namespaceIndex + $namespaceMarker.Length, $helper)", update_source)
        self.assertIn('[switch]$PreferCurrentMarker', update_source)
        self.assertIn("GLint ClampWebGL2PersonaLimit", update_source)
        self.assertIn("GLint ClampPersonaLimit", update_source)
        self.assertIn("$legacyFunctions", update_source)
        self.assertIn("$hasCurrentClamp", update_source)
        self.assertIn("[regex]::Escape($legacyFunction.Name)", update_source)
        self.assertIn("$content.IndexOf('{', $start)", update_source)
        self.assertIn("GLint WebGL2PersonaVaryingVectors", update_source)
        self.assertIn("kUseMobileUserAgent", update_source)
        self.assertIn("components\\embedder_support\\user_agent_utils.cc", update_source)
        self.assertIn("BUILDFLAG\\(IS_ANDROID\\)", update_source)
        self.assertIn("$($match.Groups[1].Value)", update_source)
        self.assertIn("-PreferCurrentMarker", update_source)

    def test_webgl1_persona_includes_uxr_config(self):
        patch = WEBGL1_PERSONA_PATCH.read_text(encoding="utf-8")
        self.assertIn('+#include "base/uxr_config.h"', patch)
        self.assertIn("base::UxrConfig::GetInstance()", patch)

        update_source = RESTORED_SOURCE_UPDATE.read_text(encoding="utf-8")
        self.assertIn("$content.Contains('base::UxrConfig::GetInstance()')", update_source)
        self.assertIn("-not $content.Contains('#include \"base/uxr_config.h\"')", update_source)
        self.assertIn(
            "$anchor, $anchor + \"`n\" + '#include \"base/uxr_config.h\"'",
            update_source,
        )

    def test_canvas2d_bridge_patch_closes_readback_scope(self):
        patch = CANVAS2D_BRIDGE_PATCH.read_text(encoding="utf-8")
        bridge_block = re.search(
            r"\+  if \(auto\* bridge = canvas_bridge::CanvasBridgeClient::Get\(\);"
            r".*?\n   // Read pixels into \|image_data\|\.",
            patch,
            re.DOTALL,
        )
        self.assertIsNotNone(bridge_block)
        block = bridge_block.group(0)
        self.assertIn("+      SkPixmap pixmap = image_data->GetSkPixmap();", block)
        self.assertRegex(block, r"\+    \}\n\+  \}\n\+\n   // Read pixels")
        self.assertIn("TextMetrics* BaseRenderingContext2D::measureText", patch)
        self.assertIn("bridge->RequestTextMetrics", patch)
        self.assertLess(
            patch.index("bridge->RequestTextMetrics"),
            patch.index("Scale text metrics if enabled"),
        )

        update_source = RESTORED_SOURCE_UPDATE.read_text(encoding="utf-8")
        migration = re.search(
            r'-RelativePath "third_party\\blink\\renderer\\modules\\canvas\\canvas2d\\base_rendering_context_2d\.cc".*?'
            r"-OldText @'\n(.*?)\n'@ `\n  -NewText @'\n(.*?)\n'@",
            update_source,
            re.DOTALL,
        )
        self.assertIsNotNone(migration)
        old_text, new_text = migration.groups()
        self.assertNotIn(old_text, new_text)
        malformed = f"prefix\r\n{old_text.replace(chr(10), chr(13) + chr(10))}\r\nsuffix"
        normalized = malformed.replace("\r\n", "\n")
        repaired = normalized.replace(old_text, new_text)
        self.assertNotEqual(repaired, normalized)
        self.assertEqual(repaired.replace(old_text, new_text), repaired)
        self.assertIn("    }\n  }", new_text)

    def test_resume_accepts_historical_webgl_persona_markers(self):
        update_source = RESTORED_SOURCE_UPDATE.read_text(encoding="utf-8")
        self.assertIn('[string[]]$CurrentMarker = @()', update_source)
        current = update_source.rindex('$normalizedContent.Contains($normalizedNewText)')
        marker = update_source.rindex('foreach ($marker in $CurrentMarker)')
        stale = update_source.index('$normalizedContent.Contains($normalizedOldText)')
        self.assertGreater(current, stale)
        self.assertGreater(marker, stale)
        self.assertIn("'String(renderer.c_str())'", update_source)
        self.assertIn("'String(WebGLPersonaRenderer().c_str())'", update_source)
        self.assertIn("'const std::string renderer = config.Get(\"uxr-webgl-renderer\");'", update_source)
        self.assertIn("'String(vendor.c_str())'", update_source)
        self.assertIn("'String(WebGLPersonaVendor().c_str())'", update_source)
        self.assertIn("'const std::string vendor = config.Get(\"uxr-webgl-vendor\");'", update_source)
        self.assertIn(
            "-CurrentMarker 'String(\"WebGL GLSL ES 3.00 "
            "(OpenGL ES GLSL ES 3.0 Chromium)\")'",
            update_source,
        )

    def test_resume_preserves_cache_but_discards_incompatible_chromium_source(self):
        stage = CI_STAGE.read_text(encoding="utf-8")
        self.assertIn('$unpackedMarker = Join-Path $Src ".chromix-source-unpacked"', stage)
        self.assertIn('$readyMarker = Join-Path $Src ".chromix-source-ready"', stage)
        self.assertIn('$restoredVersion -ne $Revisions.ChromiumVersion', stage)
        self.assertIn(
            'preserving tooling/download_cache and removing incompatible src/out',
            stage,
        )
        self.assertIn('Remove-Item $Src -Recurse -Force', stage)
        self.assertRegex(
            stage,
            r'(?s)if \(\$restoredVersion -and .*?\) \{.*?Remove-Item \$Src '
            r'-Recurse -Force\s+\} elseif \(Test-Path \$readyMarker\) \{\s+'
            r'\$MigrateRestoredSource = -not \$RestoredUpstream',
        )

    def test_resume_defers_source_migrations_for_interrupted_patch_layers(self):
        stage = CI_STAGE.read_text(encoding="utf-8")
        self.assertIn("elseif (Test-Path $readyMarker)", stage)
        self.assertIn(
            "restored source is not ready; deferring migrations until patch preparation completes",
            stage,
        )
        restore = stage.index('& $sevenZip x "C:\\restore\\tree.7z.001"')
        ready_gate = stage.index("elseif (Test-Path $readyMarker)", restore)
        migration = stage.index("update-restored-source.ps1", ready_gate)
        prepare = stage.index('prepare-ungoogled.ps1', ready_gate)
        self.assertLess(ready_gate, prepare)
        self.assertLess(prepare, migration)

    def test_interrupted_chromix_patch_layer_resumes_without_discarding_source(self):
        prepare = PREPARE_UNGOOGLED.read_text(encoding="utf-8")
        self.assertIn('$interruptedLayer -eq "chromix"', prepare)
        self.assertIn("retrying interrupted Chromix patch application in place", prepare)
        self.assertIn("--reverse --force", prepare)
        self.assertIn("--reverse --dry-run", prepare)
        self.assertIn("--forward --dry-run", prepare)
        self.assertIn(".chromix-patch-in-progress", prepare)
        self.assertIn('Set-Marker ".chromix-patch-in-progress" "$rel|$patchHash"', prepare)
        self.assertIn("patch content changed since the interrupted attempt", prepare)
        self.assertIn("interrupted patch content changed and the new patch cannot apply cleanly", prepare)
        self.assertIn("interrupted patch had not changed the source", prepare)
        self.assertIn("inferred legacy interrupted patch", prepare)
        self.assertIn("chromium-152-webgl-0082-partial.patch", prepare)
        self.assertIn("$resumeChromixPatchIsClean = $true", prepare)
        self.assertIn("legacy WebGL rollback target already present", prepare)
        self.assertIn("rolled-back patch applied", prepare)
        self.assertIn("skipping completed $rel", prepare)
        self.assertIn("interrupted patch rolled back and reapplied", prepare)
        self.assertIn("RecordWebGLOp(63u", prepare)
        forward_check = prepare.index('& $PatchExe -p1 --batch --forward --dry-run -i $patch')
        reverse_check = prepare.index('& $PatchExe -p1 --batch --reverse --dry-run -i $patch', forward_check)
        reverse_recovery = prepare.index('& $PatchExe -p1 --batch --reverse --force -i $patch', reverse_check)
        normal_forward = prepare.index('& $PatchExe -p1 --batch --forward -i $patch', forward_check)
        self.assertLess(forward_check, reverse_check)
        self.assertLess(reverse_check, reverse_recovery)
        self.assertIn('Get-ChildItem $Src -Filter "*.rej"', prepare)
        recovery = prepare.index('$interruptedLayer -eq "chromix"')
        discard = prepare.index("Remove-Item $Src -Recurse -Force", recovery)
        else_branch = prepare.index("} else {", recovery)
        self.assertGreater(discard, else_branch)

    def test_timezone_patch_matches_chromium_152_include_context(self):
        patch = TIMEZONE_PATCH.read_text(encoding="utf-8")
        self.assertIn('#include "base/command_line.h"', patch)
        self.assertIn('+#include "base/uxr_config.h"', patch)
        self.assertNotIn('+#include "base/command_line.h"', patch)
        self.assertIn("String effective_id = timezone_id;", patch)

    def test_chromium_152_webgl_bridge_patches_use_rebased_contexts(self):
        bridge = (
            REPO
            / "patches"
            / "0082-third_party-blink-renderer-modules-webgl-webgl_rendering_context_base-cc.patch"
        ).read_text(encoding="utf-8")
        lifecycle = (REPO / "patches" / "0099-webgl-bridge-lifecycle-cc.patch").read_text(
            encoding="utf-8"
        )
        readback = (REPO / "patches" / "0100-webgl-readback-noise.patch").read_text(
            encoding="utf-8"
        )
        fingerprint = (
            REPO / "patches" / "0108-webgl-gpu-fingerprint-integration.patch"
        ).read_text(encoding="utf-8")
        self.assertIn("RecordWebGLOp(63u", bridge)
        self.assertIn("RecordWebGLOp(50u", bridge)
        self.assertNotIn("kBridgeDisabledCanvasId", bridge)
        self.assertIn("canvas_id == kBridgeDisabledCanvasId", lifecycle)
        self.assertIn("bridge_substituted = true", readback)
        self.assertIn("Preserve the native query path", fingerprint)
        self.assertIn("GetGLRendererStringForFingerprint", fingerprint)

    def test_final_windows_bundle_is_verified_before_upload(self):
        stage = CI_STAGE.read_text(encoding="utf-8")
        self.assertIn("function Verify-FinalBundle", stage)
        self.assertIn("Get-FileHash $asset -Algorithm SHA256", stage)
        self.assertIn("Expand-Archive -LiteralPath $asset", stage)
        self.assertIn('"chromix.cmd"', stage)
        self.assertIn('"chrome.exe"', stage)
        self.assertIn('data:text/html,<p>chromix-smoke-ok</p>', stage)
        self.assertIn("Invoke-BoundedBrowser", stage)
        self.assertLess(stage.index("Verify-FinalBundle"), stage.index("Write-OutVar finished true"))

    def test_windows_reusable_workflow_is_available(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_call:", workflow)
        self.assertIn("name: chromix-win-x64", workflow)

    def test_resume_passes_output_directory_and_invalidates_webgl_objects(self):
        stage = CI_STAGE.read_text(encoding="utf-8")
        update = RESTORED_SOURCE_UPDATE.read_text(encoding="utf-8")
        self.assertIn('update-restored-source.ps1" -Src $Src -OutDir $OutDir', stage)
        self.assertIn('chromix-renderer-objects-invalidated.txt', update)
        self.assertIn('removed $($staleObjects.Count) restored renderer object files', update)
        self.assertIn('SetLastWriteTimeUtc', update)
        self.assertIn(
            'third_party\\blink\\renderer\\modules\\webgl\\webgl_rendering_context_base.cc',
            update,
        )
        self.assertIn(
            'third_party\\blink\\renderer\\modules\\webgl\\webgl2_rendering_context_base.cc',
            update,
        )


class WindowsPruningRegressionTest(unittest.TestCase):
    def prune_options(self):
        prepare = PREPARE_UNGOOGLED.read_text(encoding="utf-8")
        call = re.search(
            r'Invoke-Checked \$Python @\(\s*'
            r'\(Join-Path \$Ungoogled "utils\\prune_binaries\.py"\),'
            r'(?P<options>.*?)\$Src, '
            r'\(Join-Path \$Ungoogled "pruning\.list"\)\s*\)',
            prepare,
            re.DOTALL,
        )
        self.assertIsNotNone(call)
        return re.findall(r'"(--[^"\s]+)"', call["options"])

    def test_pruning_preserves_installed_tools_but_still_uses_pruning_list(self):
        self.assertEqual(self.prune_options(), ["--keep-contingent-paths"])

    def run_prune_fixture(self, options):
        import subprocess
        import sys
        import tempfile

        pruner = (REPO / ".chromix-build-verify/tooling/ungoogled-chromium"
                  / "utils/prune_binaries.py")
        if not pruner.is_file():
            self.skipTest("pinned ungoogled-chromium verification tooling is unavailable")
        temp = tempfile.TemporaryDirectory(prefix="chromix windows prune ")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        src = root / "src"
        tools = [
            "third_party/rust-toolchain/bin/cargo.exe",
            "third_party/rust-toolchain/bin/rustc.exe",
            "third_party/rust-toolchain/lib/rustlib/x86_64-pc-windows-msvc/lib/std.rlib",
            "third_party/llvm-build/Release+Asserts/bin/clang-cl.exe",
            "third_party/ninja/ninja.exe",
            "third_party/devtools-frontend/src/third_party/esbuild/esbuild.exe",
        ]
        for relative in tools:
            path = src / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"installed toolchain fixture")
        unwanted = src / "unneeded.bin"
        unwanted.write_bytes(b"prune this listed binary")
        pruning_list = root / "pruning.list"
        pruning_list.write_text("unneeded.bin\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", str(pruner), *options, str(src), str(pruning_list)],
            cwd=root, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(unwanted.exists())
        return src, tools, result

    def test_old_invocation_removes_toolchains_despite_absent_diagnostics(self):
        src, tools, result = self.run_prune_fixture([])
        self.assertIn("Absent: third_party/rust-toolchain/", result.stderr)
        for relative in tools:
            with self.subTest(path=relative):
                self.assertFalse((src / relative).exists())

    def test_prepare_invocation_keeps_toolchains_through_pruning(self):
        src, tools, result = self.run_prune_fixture(self.prune_options())
        self.assertIn("Keeping Contingent Paths", result.stderr)
        for relative in tools:
            with self.subTest(path=relative):
                self.assertEqual((src / relative).read_bytes(), b"installed toolchain fixture")


class RustToolchainMergeRegressionTest(unittest.TestCase):
    """The first real CI run died at build_bindgen.py: "Missing cargo".

    Root cause class: prepare ran under Windows PowerShell 5.1 on GitHub, and
    the Prepare-RustToolchain PowerShell transcription of upstream's merge
    silently produced a tree without bin/cargo.exe (every pwsh 7 replication
    of it looked perfect). These tests keep every layer of the fix in place:
    a Python merge ported verbatim from ungoogled-chromium-windows' field-
    proven build.py, hard verification that cargo and rustc actually landed,
    diagnostics printing bundle and merged-entry listings on failure, and a
    bindgen precondition in ci-stage.ps1 so this failure mode dies loudly
    before ninja bootstrap burns forty minutes.
    """

    MERGE_PY = REPO / "build" / "windows" / "prep_rust_toolchain.py"

    def test_wrapper_delegates_to_python_port_and_verifies_result(self):
        prepare = PREPARE_UNGOOGLED.read_text(encoding="utf-8")
        wrapper = prepare[
            prepare.index("function Prepare-RustToolchain {"):
            prepare.index("function Restore-LiteTarballFiles")
        ]
        # The silent PS 5.1 copy loop must stay gone; only the upstream-python
        # port performs the merge now.
        self.assertNotIn("Copy-Item", wrapper)
        self.assertIn('Join-Path $Repo "build\\windows\\prep_rust_toolchain.py"', wrapper)
        self.assertIn('"--third-party-root", (Join-Path $Src "third_party")', wrapper)
        # Defense in depth: verify independently of the python exit code and
        # dump every rust-toolchain directory when verification fails.
        self.assertIn('if (-not (Test-Path (Join-Path $destination "bin\\$binary")))',
                      wrapper)
        self.assertIn('Where-Object { $_.Name -like "rust-toolchain*" }', wrapper)
        self.assertIn('throw "Rust toolchain merge did not produce bin\\$binary"', wrapper)

    def test_ci_stage_fails_fast_with_diagnostics_when_cargo_is_missing(self):
        stage = CI_STAGE.read_text(encoding="utf-8")
        guard_start = stage.index(
            'if (-not (Test-Path "third_party\\rust-toolchain\\bin\\bindgen.exe")) {')
        bindgen_call = stage.index("python tools\\rust\\build_bindgen.py", guard_start)
        guard = stage[guard_start:bindgen_call]
        self.assertIn('if (-not (Test-Path "third_party\\rust-toolchain\\bin\\$binary"))',
                      guard)
        self.assertIn(
            'throw ("bindgen precondition failed: '
            'third_party\\rust-toolchain\\bin\\$binary is missing")', guard)
        # The precondition must gate the bindgen invocation itself.
        guard_end = stage.index('\\$binary is missing")', guard_start)
        self.assertLess(guard_end, bindgen_call)

    def test_merge_port_keeps_upstream_semantics_and_verifies_before_stamping(self):
        source = self.MERGE_PY.read_text(encoding="utf-8")
        # Upstream merge semantics: bin+lib from component dirs, host-bin from
        # x64 only on 64-bit hosts, version stamp via rustc --version.
        self.assertIn('DIRS_TO_COPY = ["bin", "lib"]', source)
        self.assertIn('(part == "bin") and', source)
        # The condition spans three lines in the port; match the inner test,
        # which is what locks the upstream x64-only-bin rule.
        self.assertIn('host_is_64bit != (source.name == "rust-toolchain-x64")', source)
        self.assertIn('shutil.copytree(cp_src, cp_dst, dirs_exist_ok=True)', source)
        self.assertIn('subprocess.run([str(rustc), "--version"], stdout=handle, check=True)', source)
        # Missing binaries must fail with an inventory even where executing
        # the bundled rustc.exe is impossible: verify runs before stamping.
        self.assertIn('BINARIES_THAT_MUST_EXIST = ["cargo.exe", "rustc.exe"]', source)
        order_verify = source.index("merge_toolchain(sources, destination)")
        order_stamp = source.index("write_installed_version(destination, sources)")
        self.assertLess(order_verify, order_stamp)
        # Absolute-path binding keeps "--third-party-root ." meaningful.
        self.assertIn('root = Path(args.third_party_root or ".").resolve()', source)

    def test_merge_port_executes_against_a_real_bundle_layout(self):
        import os
        import shutil
        import subprocess
        import tempfile

        if not os.path.exists("/bin/true"):
            self.skipTest("/bin/true not available")

        temp = tempfile.TemporaryDirectory(prefix="chromix rust merge ")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "tp"
        x64 = root / "rust-toolchain-x64"
        arm = root / "rust-toolchain-arm"
        # Component-directory layout of the pinned rust nightly bundles:
        # executables live under <component>/bin, libraries under
        # <component>/lib.
        for base in (x64, arm):
            for part in ("bin", "lib"):
                payload = base / f"rustc/{part}"
                payload.mkdir(parents=True)
                shutil.copy("/bin/true", payload / "cargo.exe")
            if base is x64:
                shutil.copy("/bin/true", base / "rustc/bin/rustc.exe")

        result = subprocess.run(
            ["python3", str(self.MERGE_PY), "--third-party-root", str(root)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        merged = root / "rust-toolchain"
        # Host executables come from the bundles' component dirs...
        self.assertTrue((merged / "bin/cargo.exe").is_file())
        self.assertTrue((merged / "bin/rustc.exe").is_file())
        # ...and lib payloads merged from every available bundle, mirroring
        # the glob */{bin,lib}/* semantics of upstream's merge.
        self.assertTrue((merged / "lib/cargo.exe").is_file())
        self.assertEqual(result.stdout.strip().splitlines()[-1],
                         f"==> merged Rust toolchain: 2 bundles -> {merged}")
        self.assertTrue((merged / "INSTALLED_VERSION").exists())

    def test_merge_port_reports_missing_x64_bundle_loudly(self):
        import subprocess
        import tempfile

        temp = tempfile.TemporaryDirectory(prefix="chromix rust merge bad ")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "tp"
        result = subprocess.run(
            ["python3", str(self.MERGE_PY), "--third-party-root", str(root)],
            capture_output=True, text=True)
        # No x64 bundle present must fail with the inventory rather than an
        # unhandled traceback or - worse - a silent zero exit like PS 5.1 did.
        self.assertNotEqual(result.returncode, 0)
        combined = result.stderr + result.stdout
        self.assertIn("no downloaded x64 rust bundle", combined)


if __name__ == "__main__":
    unittest.main()
