import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PACKAGE_MACOS = REPO / "build" / "macos" / "package-macos.sh"
PREPARE = REPO / "build" / "prepare-ungoogled.sh"
WORKFLOW = REPO / ".github" / "workflows" / "build-cross-platform.yml"
REVISIONS = REPO / "build" / "ungoogled-revisions.psd1"


class CrossPlatformBuildRegressionTest(unittest.TestCase):
    def test_prepare_uses_pinned_core_archive_and_platform_layers(self):
        source = PREPARE.read_text(encoding="utf-8")
        self.assertIn("PLATFORM_KEY=UngoogledLinux", source)
        self.assertIn("PLATFORM_KEY=UngoogledMacOS", source)
        self.assertIn('utils/downloads.py" retrieve', source)
        self.assertIn('utils/downloads.py" unpack', source)
        self.assertIn('utils/prune_binaries.py', source)
        self.assertIn('utils/patches.py" apply "$SRC" "$CORE_REPO/patches"', source)
        self.assertIn('utils/patches.py" apply "$SRC" "$PLATFORM_PATCHES"', source)
        self.assertLess(source.index('utils/patches.py" apply "$SRC" "$CORE_REPO/patches"'), source.index('utils/patches.py" apply "$SRC" "$PLATFORM_PATCHES"'))
        self.assertLess(source.index('utils/patches.py" apply "$SRC" "$PLATFORM_PATCHES"'), source.index('utils/prune_binaries.py'))
        self.assertLess(source.index('utils/prune_binaries.py'), source.index('"$REPO/build/apply-patches.sh" "$SRC"'))
        self.assertIn('"$REPO/build/apply-patches.sh" "$SRC"', source)
        self.assertNotIn("fetch --nohooks", source)
        self.assertNotIn("gclient sync", source)

    def test_domain_substitution_runs_after_platform_tool_downloads(self):
        linux = (REPO / "build" / "build.sh").read_text(encoding="utf-8")
        macos = (REPO / "build" / "macos" / "build.sh").read_text(encoding="utf-8")
        for source in (linux, macos):
            self.assertIn("CHROMIX_APPLY_DOMAIN_SUBSTITUTION:-1", source)
            self.assertIn("utils/domain_substitution.py", source)

    def test_platform_commits_are_explicitly_pinned(self):
        source = REVISIONS.read_text(encoding="utf-8")
        self.assertIn(
            'UngoogledLinuxCommit = "02c59ed68d1963a647bb478064823d114e466ffb"',
            source,
        )
        self.assertIn(
            'UngoogledMacOSCommit = "038db2b41f7aeb00bbceb2f5a56912b26eb5b284"',
            source,
        )

    def test_workflow_matches_sdk_asset_names(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        posix_workflow = REPO / ".github" / "workflows" / "build-posix-github.yml"
        posix_source = posix_workflow.read_text(encoding="utf-8")
        for asset in (
            "chromix-linux-x64",
            "chromix-linux-arm64",
            "chromix-mac-x64",
            "chromix-mac-arm64",
        ):
            self.assertIn(asset, source)
        # The POSIX reusable workflow receives the artifact name as an input.
        self.assertIn("${{ inputs.artifact }}", posix_source)
        self.assertIn("artifact:", source)
        # All five targets are reusable staged workflows now; runner names,
        # caches, and stage chains live in the called workflow files.
        self.assertNotIn("macos-14", source)
        self.assertIn("actions/cache@v4", posix_source)
        self.assertIn("download_cache", posix_source)
        self.assertIn("./.github/workflows/build-win-x64-github.yml", source)
        self.assertIn("12-stage snapshot/resume", source)
        self.assertIn("8-stage snapshot/resume", source)
        self.assertIn(".github/workflows/build-posix-github.yml", source)

    def test_build_arguments_cover_host_toolchain_compatibility(self):
        linux = (REPO / "build" / "build.sh").read_text(encoding="utf-8")
        macos = (REPO / "build" / "args.macos.gn").read_text(encoding="utf-8")
        self.assertIn("-Wno-deprecated-declarations", linux)
        self.assertIn("use_unified_system_module = false", macos)

    def test_macos_packager_normalizes_intel_name_and_uses_portable_tools(self):
        source = PACKAGE_MACOS.read_text(encoding="utf-8")
        self.assertRegex(source, r'ARCH="\$\{3:-\$\(uname -m\)\}"')
        self.assertIn('x86_64|amd64) ARCH=x64', source)
        self.assertNotIn("GNU tar is required", source)
        self.assertIn('zip -X -q -r', source)
        self.assertIn('shasum -a 256', source)
        self.assertIn("CHROMIX_CHROMIUM_LICENSE", source)
        self.assertIn("LICENSE.chromix", source)
        self.assertIn("LICENSE.chromium", source)


if __name__ == "__main__":
    unittest.main()
