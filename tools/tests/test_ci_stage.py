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


class ResumeWorkflowRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")

    def test_resume_skips_predecessors_and_starts_requested_stage(self):
        self.assertIn("resume_run_id:", self.source)
        self.assertIn(
            "if: ${{ inputs.resume_run_id == '' && inputs.upstream_run_id == '' }}",
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
        self.assertEqual(self.source.count("if: ${{ always() }}"), 48)
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

    def test_upstream_cache_skips_standalone_validation_and_imports_stage_one(self):
        self.assertIn("upstream_run_id:", self.source)
        self.assertIn("inputs.resume_run_id == '' && inputs.upstream_run_id == ''", self.source)
        self.assertIn("repository: ungoogled-software/ungoogled-chromium-windows", self.source)
        self.assertIn("name: build-artifact", self.source)
        self.assertIn("github-token: ${{ secrets.UPSTREAM_ACTIONS_TOKEN }}", self.source)
        self.assertIn("-UpstreamArtifactPath C:\\upstream", self.source)

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
    def test_upstream_artifact_import_reuses_source_and_objects_before_chromix_patches(self):
        stage = CI_STAGE.read_text(encoding="utf-8")
        self.assertIn('[string]$UpstreamArtifactPath = ""', stage)
        self.assertIn('Join-Path $UpstreamArtifactPath "artifacts.zip"', stage)
        self.assertIn('Join-Path $WorkDir "src"', stage)
        self.assertIn('Join-Path $WorkDir "build\\src"', stage)
        self.assertIn('upstream artifact is missing src/BUILD.gn', stage)
        self.assertIn('Join-Path $upstreamSrc "chrome\\VERSION"', stage)
        self.assertIn('$upstreamVersion -ne $Revisions.ChromiumVersion', stage)
        self.assertIn('upstream artifact targets Chromium $upstreamVersion', stage)
        self.assertIn('if ((Resolve-Path $upstreamSrc).Path -ne $Src)', stage)
        self.assertIn('Move-Item $upstreamSrc $Src', stage)
        self.assertIn('Join-Path $Src "out\\Default"', stage)
        self.assertIn('Move-Item $upstreamOut $OutDir', stage)
        self.assertIn('Set-Content -Path (Join-Path $Src ".chromix-ungoogled-core")', stage)
        self.assertIn('Set-Content -Path (Join-Path $Src ".chromix-ungoogled-windows")', stage)
        imported = stage.index('if ($UpstreamArtifactPath) {')
        prepare = stage.index('prepare-ungoogled.ps1', imported)
        self.assertLess(imported, prepare)

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
        prepare = stage_source.index('.chromix-source-ready', update)
        self.assertLess(restore, update)
        self.assertLess(update, prepare)
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
            r'& "\$PSScriptRoot\\update-restored-source\.ps1"',
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
        prepare = stage.index("prepare-ungoogled.ps1", migration)
        self.assertLess(ready_gate, migration)
        self.assertLess(migration, prepare)

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
