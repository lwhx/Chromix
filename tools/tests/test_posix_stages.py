"""Regression tests for the upstream-modeled POSIX staged CI scripts and workflow."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

CI_STAGE = REPO / "build" / "posix" / "ci-stage.sh"
CI_PARTS = REPO / "build" / "posix" / "ci-parts.sh"
GEN_WORKFLOW = REPO / "tools" / "gen_posix_workflow.py"
WORKFLOW = REPO / ".github" / "workflows" / "build-posix-github.yml"
MAIN_WORKFLOW = REPO / ".github" / "workflows" / "build-cross-platform.yml"


class PosixStageSyntaxTest(unittest.TestCase):
    def test_ci_scripts_are_executable_and_parse(self):
        for script in (CI_STAGE, CI_PARTS):
            self.assertTrue(script.is_file(), script)
            self.assertEqual(os.stat(script).st_mode & stat.S_IXUSR, stat.S_IXUSR)
            result = subprocess.run(["bash", "-n", str(script)],
                                     capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(shutil.which("zstd"), "zstd required")
class PosixSnapshotRoundTripTest(unittest.TestCase):
    """ci-parts.sh must pack tar|zstd volumes that ci-stage.sh can restore."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromix posix ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        (self.work / "src").mkdir(parents=True)
        marker = self.work / "src/.chromix-source-ready"
        marker.write_text("linux|x64|152.0.7977.82|core|platform|patchhash\n")
        executable = self.work / "src/tool"
        executable.write_text("#!/bin/sh\necho ok\n")
        executable.chmod(0o755)
        (self.work / "link-target").write_text("fixture")
        (self.work / "src/symlink").symlink_to("../link-target")
        # A small volume size forces the multi-volume slicing path in tests.
        self.env = {
            **os.environ,
            "CHROMIX_SNAPSHOT_VOLUME_BYTES": str(64 * 1024),
            "CHROMIX_SNAPSHOT_MAX_VOLUMES": "8",
        }

    def round_trip_payload(self, repeat):
        # Random bytes defeat zstd's compression, so volume counts follow
        # CHROMIX_SNAPSHOT_VOLUME_BYTES exactly as they do in production.
        rng = __import__("random").Random(0)
        return bytes(rng.getrandbits(8) for _ in range(64 * 1024 * repeat))

    def snapshot(self, extra_env=None):
        parts_dir = self.root / f"parts-{len(list(self.root.iterdir()))}"
        result = subprocess.run(
            ["bash", str(CI_PARTS), str(self.work), str(parts_dir)],
            capture_output=True, text=True,
            env={**self.env, **(extra_env or {})})
        return parts_dir, result

    def restore(self, parts_dir, dest):
        # The real chain merges every slot artifact into one directory before
        # sorting; volumes only compare correctly by their numeric suffix.
        archives = sorted(
            (p for p in parts_dir.rglob("tree.tar.zst.*")
             if p.name != "tree.tar.zst."),
            key=lambda p: int(p.name.rsplit(".", 1)[1]))
        self.assertTrue(archives)
        with subprocess.Popen(
            ["zstd", "-d", "-T0"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE) as zstd_proc:
            for archive in archives:
                zstd_proc.stdin.write(archive.read_bytes())
            zstd_proc.stdin.close()
            extract = subprocess.run(
                ["tar", "-xpf", "-", "-C", str(dest)],
                stdin=zstd_proc.stdout, capture_output=True)
            zstd_proc.wait(timeout=60)
        self.assertEqual(extract.returncode, 0, extract.stderr)

    def test_round_trip_preserves_modes_symlinks_and_markers(self):
        # ~384 KiB of random bytes against 64 KiB volumes exercises multi-volume
        # split, round-robin wraparound across all four slots, and ordered
        # restore. Random data keeps zstd from collapsing everything into the
        # single-volume path that hid slicing bugs with compressible fixtures.
        payload = self.round_trip_payload(6)
        self.assertEqual(len(payload), 64 * 1024 * 6)
        (self.work / "src/payload.bin").write_bytes(payload)
        parts_dir, result = self.snapshot()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        volumes = sorted(
            (p for p in parts_dir.rglob("tree.tar.zst.*")
             if p.name != "tree.tar.zst."),
            key=lambda p: int(p.name.rsplit(".", 1)[1]))
        self.assertGreater(len(volumes), 4)
        self.assertLessEqual(len(volumes), 8)
        slots = {v.parent.name for v in volumes}
        self.assertEqual(slots, {"p1", "p2", "p3", "p4"})
        restored = self.root / "restored"
        restored.mkdir()
        self.restore(parts_dir, restored)
        tool = restored / "src/tool"
        self.assertTrue(tool.is_file())
        self.assertEqual(tool.stat().st_mode & stat.S_IXUSR, stat.S_IXUSR)
        self.assertTrue((restored / "src/symlink").is_symlink())
        self.assertEqual((restored / "src/symlink").read_text(), "fixture")
        marker_key = (self.work / "src/.chromix-source-ready").read_text()
        self.assertEqual((restored / "src/.chromix-source-ready").read_text(),
                         marker_key)
        self.assertEqual((restored / "src/payload.bin").read_bytes(), payload)

    def test_exceeding_the_volume_budget_aborts_instead_of_uploading_broken_state(self):
        # Three 64 KiB volumes of random data against a two-volume budget must
        # fail the stage and leave no partial upload directories behind.
        (self.work / "src/payload.bin").write_bytes(self.round_trip_payload(3))
        parts_dir, result = self.snapshot(
            {"CHROMIX_SNAPSHOT_MAX_VOLUMES": "2"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("handoff budget", result.stdout + result.stderr)
        self.assertEqual(list(parts_dir.rglob("tree.tar.zst.*")), [])

    def test_more_than_four_volumes_warns_about_plan_size_caps(self):
        # Between MAX_SLOTS and MAX_VOLUMES the chain warns but keeps going:
        # re-packing hundreds of gigabytes buys nothing when the artifact
        # upload cap is the real constraint.
        (self.work / "src/payload.bin").write_bytes(self.round_trip_payload(6))
        parts_dir, result = self.snapshot()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        volumes = list(parts_dir.rglob("tree.tar.zst.*"))
        self.assertGreater(len(volumes), 4)
        self.assertLessEqual(len(volumes), 8)
        output = result.stdout + result.stderr
        self.assertIn("::warning::", output)
        self.assertIn("artifact size caps depend on the GitHub plan", output)

    def test_stage_chain_keeps_upstream_guards_in_source(self):
        stage_source = CI_STAGE.read_text(encoding="utf-8")
        # Mirror of the Windows last-stage guard: without it a chain that runs
        # out of stages would end green with no artifact and release nothing.
        self.assertIn('if [ "$STAGE_INDEX" -ge "$MAX_STAGES" ]', stage_source)
        self.assertIn(
            'die "stage $STAGE_INDEX reached max-stages $MAX_STAGES without finishing"',
            stage_source)
        # Domain substitution must never resume over an interrupted marker.
        self.assertIn('.chromix-domain-substitution-in-progress', stage_source)
        parts_source = CI_PARTS.read_text(encoding="utf-8")
        self.assertIn('CHROMIX_SNAPSHOT_VOLUME_BYTES', parts_source)
        self.assertIn('CHROMIX_SNAPSHOT_MAX_VOLUMES', parts_source)
        self.assertIn('"$SPLIT" -a 3 -d -b "$VOLUME_BYTES"', parts_source)

    def test_stage_chain_is_macos_bash_3_2_compatible(self):
        stage_source = CI_STAGE.read_text(encoding="utf-8")
        # macOS runners execute workflow steps with the system /bin/bash
        # 3.2. Three first-run failure classes came from treating this like a
        # modern bash or a GNU-only Linux toolchain:
        self.assertIn(
            'REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"',
            stage_source)
        # 1. $(( )) cannot nest quoted command substitution on bash 3.2
        #    (macos stage 1 died at remaining_min); expand to variables.
        self.assertNotIn('- "$(now_epoch)")', stage_source)
        self.assertNotIn("- \"$(now_epoch)\")", stage_source)
        self.assertIn('left=$(( (DEADLINE_EPOCH - now) / 60 ))',
                      stage_source)
        # 2. GNU timeout does not exist on macOS; coreutils ships gtimeout,
        #    so the ninja deadline and smoke checks must resolve it first.
        self.assertIn('elif command -v gtimeout >/dev/null 2>&1; then',
                      stage_source)
        self.assertNotIn(' timeout 30s ', stage_source)
        self.assertNotIn(' timeout 60s ', stage_source)

    def test_linux_build_selects_host_arch_tools_like_upstream_portablelinux(self):
        source = (REPO / "build" / "build.sh").read_text(encoding="utf-8")
        # Upstream setup_toolchain keys Node/Go to the host architecture;
        # the target only selects sysroots and GN args.
        self.assertIn('GO_ARCH="$HOST_ARCH"', source)
        self.assertNotIn('GO_ARCH="$ARCH"', source.replace('GO_ARCH="$HOST_ARCH"', ''))
        self.assertIn('third_party/dawn/tools/golang/linux-$GO_ARCH/bin/go', source)
        self.assertIn('SYSROOT_ARCH=amd64', source)
        self.assertIn('SYSROOT_ARCH=arm64', source)


@unittest.skipUnless(
    Path(os.path.expanduser("~/.local/bash-3.2-for-ci/bash")).is_file(),
    "locally built bash 3.2 required (matches macOS /bin/bash)")
class PosixStageBash32ExecutionTest(unittest.TestCase):
    """Execute the handoff chain under real bash 3.2 like macOS runners do.

    Static checks cannot catch what this class caught in the first real run:
    quoted command substitution inside $(( )) dies on bash 3.2, GNU timeout
    does not exist on macOS, and a wrong REPO hop broke every $REPO path.
    Both stage paths below run with a deadline that forces the prepare
    budget under its minimum, exercising argument parsing, remaining_min,
    GITHUB_OUTPUT emission, ci-parts packing, cross-segment restore, and the
    second handoff - without compiling anything.
    """

    BASH32 = Path(os.path.expanduser("~/.local/bash-3.2-for-ci/bash"))

    def test_stage1_handoff_then_stage2_restore_and_rehandoff(self):
        import datetime
        base = Path(tempfile.mkdtemp(prefix="chromix bash32 "))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        work = base / "work"
        work.mkdir()
        out_file = base / "github_output"
        # remaining minutes minus reserve lands below the 25-minute minimum.
        deadline = int(datetime.datetime.now().timestamp()) + 40 * 60

        def run_stage(args):
            return subprocess.run(
                [str(self.BASH32), "--norc", str(CI_STAGE), *args],
                capture_output=True, text=True,
                env={**os.environ,
                     "CHROMIX_RESERVE_MINUTES": "45",
                     "GITHUB_OUTPUT": str(out_file)})

        stage1 = run_stage(["--platform", "macos", "--arch", "arm64",
                            "--workdir", str(work),
                            "--stage-index", "1", "--max-stages", "8",
                            "--deadline-epoch", str(deadline)])
        self.assertEqual(stage1.returncode, 0,
                         stage1.stdout + stage1.stderr)
        snap = work / ".snapshot-stage-1"
        volumes = list(snap.rglob("tree.tar.zst.*"))
        self.assertTrue(volumes)
        outputs = dict(line.split("=", 1)
                       for line in out_file.read_text().splitlines())
        self.assertEqual(outputs["status"], "running")
        self.assertEqual(outputs["finished"], "false")
        self.assertEqual(outputs["upload_snapshot"], "true")

        restore = base / "restore"
        shutil.copytree(snap, restore)
        out_file.write_text("")
        stage2 = run_stage(["--platform", "linux", "--arch", "x64",
                            "--workdir", str(work),
                            "--stage-index", "2", "--max-stages", "8",
                            "--from-snapshot", str(restore),
                            "--deadline-epoch", str(deadline)])
        self.assertEqual(stage2.returncode, 0,
                         stage2.stdout + stage2.stderr)
        # The consuming stage must remove the restore directory after
        # unpacking so RUNNER_TEMP never reuses stale volumes.
        self.assertFalse(restore.exists())
        self.assertTrue((work / ".snapshot-stage-2" / "p1").is_dir())


@unittest.skipUnless(shutil.which("zstd"), "zstd required")
class PosixStageRestoreRejectsMismatchedSnapshotTest(unittest.TestCase):
    def test_missing_snapshot_directory_fails_for_resume_stage(self):
        temp = tempfile.TemporaryDirectory(prefix="chromix posix reject ")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        fake_restore = root / "restore-missing"
        output_file = root / "github_output"
        with output_file.open("w") as out:
            proc = subprocess.run(
                ["bash", str(CI_STAGE), "--platform", "linux", "--arch",
                 "x64", "--workdir", str(root / "work"),
                 "--stage-index", "2", "--max-stages", "8",
                 "--from-snapshot", str(fake_restore),
                 "--deadline-epoch", str(1)],
                capture_output=True, text=True,
                env={**os.environ, "GITHUB_OUTPUT": str(output_file)})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not exist", proc.stderr + proc.stdout)


class GenPosixWorkflowTest(unittest.TestCase):
    def test_workflow_matches_generator_output(self):
        subprocess.run([sys.executable, str(GEN_WORKFLOW)],
                       cwd=str(REPO), check=True, capture_output=True)
        before = subprocess.run(["sha256sum", str(WORKFLOW)],
                                 capture_output=True, text=True).stdout
        subprocess.run([sys.executable, str(GEN_WORKFLOW)], cwd=str(REPO),
                       check=True, capture_output=True)
        after = subprocess.run(["sha256sum", str(WORKFLOW)],
                                capture_output=True, text=True).stdout
        self.assertEqual(before, after)

    def test_workflow_has_no_collapsed_gha_expressions(self):
        # f-strings collapse ${{ ... }} to ${ ... }, which Actions cannot
        # expand; guard the generator against that regression.
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("${ inputs.", text)
        self.assertNotIn("${ runner.", text)
        self.assertNotIn("${ steps.", text)
        for token in ("${{ inputs.platform }}", "${{ inputs.arch }}",
                      "${{ inputs['max-stages'] }}", "${{ github.run_attempt }}",
                      "${{ steps.stage.outputs.finished }}",
                      "${{ inputs.artifact }}"):
            self.assertIn(token, text)

    def test_all_run_scripts_pass_bash_n_and_stage_args_are_correct(self):
        import re
        import yaml
        data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        jobs = data["jobs"]
        self.assertEqual(list(jobs), [f"posix-{i}" for i in range(1, 9)])
        stub = re.compile(r"\$\{\{[^}]*\}\}")
        checked = 0
        for name, job in jobs.items():
            needs = job.get("needs")
            if needs:
                self.assertIn("always()", job["if"])
                self.assertIn(f"needs.{needs}.result == 'success'", job["if"])
                self.assertIn(f"needs.{needs}.outputs.finished != 'true'",
                              job["if"])
            for step in job["steps"]:
                script = step.get("run")
                if not script:
                    continue
                proc = subprocess.run(["bash", "-n", "/dev/stdin"],
                                       input=stub.sub("x", script),
                                       capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0,
                                 (name, step.get("name"), proc.stderr))
                checked += 1
        stage1 = next(s for s in jobs["posix-1"]["steps"]
                      if s.get("name") == "Run stage 1")["run"]
        stage2 = next(s for s in jobs["posix-2"]["steps"]
                      if s.get("name") == "Run stage 2")["run"]
        self.assertNotIn("--from-snapshot", stage1)
        # A dotted inputs.max_stages rendered as '' on the first real run:
        # expression property access is literal, so dashed input keys need
        # bracket syntax. Lock the rendered argument shape per stage.
        self.assertIn(
            "--stage-index 1 --max-stages '${{ inputs['max-stages'] }}' "
            '--deadline-epoch "$DEADLINE_EPOCH"', stage1)
        self.assertIn("--from-snapshot \"${RUNNER_TEMP}/chromix-restore\"", stage2)
        deps = next(s for s in jobs["posix-1"]["steps"]
                    if s.get("name") == "Install Linux build dependencies")
        # arm64 runner images carry no Go on PATH: the pinned toolchain must
        # publish /usr/local/go/bin to later steps and verify via the
        # absolute path, not a bare `go` (first real run died with exit 127).
        self.assertIn('echo "/usr/local/go/bin" >> "$GITHUB_PATH"', deps["run"])
        self.assertIn("/usr/local/go/bin/go version", deps["run"])
        last = next(s for s in jobs["posix-8"]["steps"]
                    if s.get("name") == "Run stage 8")["run"]
        self.assertIn("--stage-index 8", last)
        download = next(s for s in jobs["posix-8"]["steps"]
                        if s.get("name") == "Download tree from previous stage")
        self.assertIn("${{ inputs.artifact }}-tree-s7-attempt-*-part*",
                      download["with"]["pattern"])
        self.assertTrue(download["with"].get("merge-multiple"))
        restore = sorted(s for s in jobs["posix-3"]["steps"]
                         if s.get("name") == "Download tree from previous stage")
        self.assertEqual(len(restore), 1)
        self.assertGreaterEqual(checked, 32)

    def test_main_workflow_references_posix_reusable_jobs(self):
        source = MAIN_WORKFLOW.read_text(encoding="utf-8")
        for artifact in ("chromix-linux-x64", "chromix-linux-arm64",
                         "chromix-mac-x64", "chromix-mac-arm64"):
            self.assertIn(artifact, source)
        self.assertIn("./.github/workflows/build-posix-github.yml", source)
        self.assertIn("./.github/workflows/build-win-x64-github.yml", source)
        self.assertNotIn("runs-on: ubuntu-22.04", source)
        self.assertNotIn("runs-on: macos-15", source)
        self.assertIn("secrets: inherit", source)


class WorkflowInputIntegrityTest(unittest.TestCase):
    """Every inputs reference must hit a declared key of its own workflow.

    GitHub Actions resolves `inputs.foo` with literal property access: for a
    dashed input like max-stages only `${{ inputs['max-stages'] }}` works,
    while `${{ inputs.max_stages }}` silently renders empty. The first real
    POSIX run died on exactly that (`--max-stages ''`). This audit across all
    workflows catches the whole class, including future renames.
    """

    WORKFLOWS = REPO / ".github" / "workflows"

    def test_input_references_match_declared_input_keys(self):
        import re
        import yaml
        dotted = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_]+)")
        bracketed = re.compile(r"\$\{\{\s*inputs\['([^']+)'")
        files = sorted(self.WORKFLOWS.glob("*.yml"))
        self.assertGreaterEqual(len(files), 5)
        for path in files:
            text = path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            triggers = data.get(True) or data.get("on") or {}
            keys = set()
            for body in triggers.values():
                if isinstance(body, dict):
                    keys |= set((body.get("inputs") or {}).keys())
            refs = {m.group(1) for m in dotted.finditer(text)}
            refs |= {m.group(1) for m in bracketed.finditer(text)}
            missing = refs - keys if keys else set()
            # Workflows without declared inputs must not reference any.
            self.assertEqual(missing, set(),
                             f"{path.name}: undeclared input refs {missing}")


if __name__ == "__main__":
    unittest.main()
