"""Release orchestration tests use tiny ZIP fixtures and a mocked GitHub CLI."""
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import release_browser as release


REPO = "owner/chromix"
SHA = "a" * 40
OTHER_SHA = "b" * 40
TAG = "v1.2.3.4"
EXPECTED_WORKFLOWS = {
    "build-linux-x64": ("chromix-linux-x64",),
    "build-linux-arm64": ("chromix-linux-arm64",),
    "build-macos-x64": ("chromix-mac-x64",),
    "build-macos-arm64": ("chromix-mac-arm64",),
    "build-win-x64-github": ("chromix-win-x64",),
}


def backup_name(data):
    return "SHA256SUMS.backup." + hashlib.sha256(data).hexdigest()


def make_run(name="build-linux-x64", run_id=100, **changes):
    run = {
        "id": run_id, "run_attempt": 1, "name": name,
        "path": f".github/workflows/{name}.yml",
        "head_sha": SHA, "head_branch": "main", "event": "push",
        "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
        "status": "completed", "conclusion": "success",
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
    }
    run.update(changes)
    return run


def write_bundle(path, missing=None, extra=None, corrupt=False):
    if path.name == "chromix-win-x64.zip":
        members = ["chromix/chromix.cmd", "chromix/chrome.exe"]
    elif path.name.startswith("chromix-mac-"):
        members = ["chromix/chromix", "chromix/Chromium.app/Contents/MacOS/Chromium"]
    else:
        members = ["chromix/chromix", "chromix/chrome"]
    members += ["chromix/LICENSE.chromix", "chromix/LICENSE.chromium"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in members:
                if name != missing:
                    archive.writestr(zipfile.ZipInfo(name), "fixture")
            if extra:
                archive.writestr(zipfile.ZipInfo(extra[0]), extra[1])
    if corrupt:
        data = path.read_bytes()
        path.write_bytes(data.replace(b"fixture", b"corrupt", 1))


class ReleaseFixtureTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runs = {name: make_run(name, 100 + i) for i, name in enumerate(EXPECTED_WORKFLOWS)}
        self.pages = [{"workflow_runs": list(self.runs.values())}]
        self.release = None
        self.refs = []
        self.tags = {}
        self.downloads = []
        self.mutations = []
        self.release_files = {}
        self.artifact_errors = {}
        self.version = "1.2.3.4"
        self.source_versions = {}
        self.uploaded_files = {}
        self.notes = []
        self.fail_upload = None
        self.release_downloads = []
        self.event_run = self.runs["build-win-x64-github"]
        self.event_response = self.event_run
        self.listing_responses = []
        self.gh = self.enterContext(patch.object(release, "gh", side_effect=self.fake_gh))
        self.stdout = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))

    def fake_gh(self, *args):
        if args[:3] == ("api", "--paginate", "--slurp"):
            if re.fullmatch(f"repos/{REPO}/actions/runs\\?head_sha=[a-f0-9]{{40}}&per_page=100", args[3]):
                pages = self.listing_responses.pop(0) if self.listing_responses else self.pages
                return json.dumps(pages)
            if args[3] == f"repos/{REPO}/releases?per_page=100":
                return json.dumps([[self.release] if self.release else []])
        if args[0] == "api":
            if args[1] == f"repos/{REPO}/actions/runs/{self.event_run['id']}":
                return json.dumps(self.event_response)
            prefix = f"repos/{REPO}/contents/CHROMIUM_VERSION?ref="
            if args[1].startswith(prefix):
                self.assertIn("Accept: application/vnd.github.raw+json", args)
                return self.source_versions.get(args[1][len(prefix):], self.version)
            if args[1] == f"repos/{REPO}/git/matching-refs/tags/{TAG}":
                return json.dumps(self.refs)
            for sha, obj in self.tags.items():
                if args[1] == f"repos/{REPO}/git/tags/{sha}":
                    return json.dumps({"object": obj})
        if args[:2] == ("run", "download"):
            name = args[args.index("--name") + 1]
            dest = Path(args[args.index("--dir") + 1])
            self.assertEqual(args[args.index("--repo") + 1], REPO)
            self.downloads.append((int(args[2]), name))
            error = self.artifact_errors.get(name)
            if error == "expired":
                raise RuntimeError("Artifact expired")
            asset = dest / (name + ".zip")
            write_bundle(asset, missing="chromix/LICENSE.chromium" if error == "layout" else None,
                         corrupt=error == "corrupt")
            if error != "missing-checksum":
                checksum = "0" * 64 if error == "checksum" else release.digest(asset)
                (dest / "SHA256SUMS").write_text(f"{checksum}  {asset.name}\n")
                if error == "foreign-checksum":
                    with (dest / "SHA256SUMS").open("a") as stream:
                        stream.write(f"{'a' * 64}  chromix-linux-x64.zip\n")
            if error == "unexpected":
                (dest / "unrelated.txt").write_text("unexpected")
            return ""
        if args[:2] == ("release", "download"):
            name = args[args.index("--pattern") + 1]
            dest = Path(args[args.index("--dir") + 1])
            dest.mkdir(parents=True, exist_ok=True)
            self.assertEqual(args[args.index("--repo") + 1], REPO)
            self.assertFalse((dest / name).exists(), "gh download cannot overwrite an existing file")
            self.release_downloads.append(name)
            (dest / name).write_bytes(self.release_files[name])
            return ""
        if args[:2] in (("release", "create"), ("release", "upload"), ("release", "edit")):
            self.mutations.append(args)
            self.assertEqual(args[args.index("--repo") + 1], REPO)
            if args[1] == "create":
                self.assertIsNone(self.release)
                self.release = {
                    "tag_name": args[2], "target_commitish": args[args.index("--target") + 1],
                    "draft": "--draft" in args, "body": "", "assets": [],
                }
            elif args[1] == "upload":
                path = Path(args[3])
                if "--clobber" in args:
                    self.assertEqual(path.name, "SHA256SUMS", "ZIPs and backup assets must remain immutable")
                    self.release_files.pop(path.name, None)
                    self.release["assets"] = [asset for asset in self.release["assets"]
                                              if asset["name"] != path.name]
                elif path.name in self.release_files:
                    raise subprocess.CalledProcessError(1, ["gh", *args])
                if path.name == self.fail_upload:
                    self.fail_upload = None
                    raise subprocess.CalledProcessError(1, ["gh", *args])
                data = path.read_bytes()
                self.uploaded_files[path.name] = data
                self.release_files[path.name] = data
                self.release["assets"].append({"name": path.name})
            elif "--draft=false" in args:
                self.release["draft"] = False
                if not any(ref["ref"] == f"refs/tags/{args[2]}" for ref in self.refs):
                    self.refs.append({"ref": f"refs/tags/{args[2]}",
                                      "object": {"type": "commit", "sha": self.release["target_commitish"]}})
            if "--notes-file" in args:
                notes = Path(args[args.index("--notes-file") + 1]).read_text()
                self.notes.append(notes)
                self.release["body"] = notes
            return ""
        raise AssertionError(f"Unexpected GitHub CLI invocation: {args}")

    def run_main(self, *args):
        event_path = self.root / "event.json"
        event_path.write_text(json.dumps({"workflow_run": self.event_run}))
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_PATH": str(event_path),
                                     "GITHUB_OUTPUT": str(self.root / "output")}):
            release.main(list(args))

    def bundles(self):
        bundles = {}
        for name in sorted(release.ASSETS):
            path = self.root / "bundles" / name
            write_bundle(path)
            bundles[name] = path
        return bundles

    def incoming(self):
        name = EXPECTED_WORKFLOWS[self.event_run["name"]][0] + ".zip"
        path = self.root / "incoming" / name
        write_bundle(path)
        return {name: path}

    def existing_release(self, bundles, draft=False, sha=SHA):
        self.refs = [{"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": sha}}]
        self.release_files = {name: path.read_bytes() for name, path in bundles.items()}
        self.release_files["SHA256SUMS"] = "".join(
            f"{release.digest(path)}  {name}\n" for name, path in sorted(bundles.items())
        ).encode("ascii")
        self.release = {
            "tag_name": TAG, "target_commitish": sha, "draft": draft,
            "body": f"Existing release\nSource commit: `{sha}`\n",
            "assets": [{"name": name} for name in self.release_files],
        }


class RunSelectionTest(ReleaseFixtureTest):
    def test_platform_workflows_each_own_exactly_one_asset(self):
        self.assertEqual(release.WORKFLOWS, EXPECTED_WORKFLOWS)
        self.assertEqual(release.ASSETS, {names[0] + ".zip" for names in EXPECTED_WORKFLOWS.values()})

    def test_workflow_subscribes_to_five_successful_main_builds(self):
        path = Path(__file__).resolve().parents[2] / ".github/workflows/release-browser.yml"
        source = path.read_text()
        workflows = re.search(r"    workflows:\n(.*?)    types:", source, re.DOTALL)[1]
        self.assertEqual([line.strip()[2:] for line in workflows.splitlines()], list(EXPECTED_WORKFLOWS))
        self.assertIn("types: [completed]", source)
        self.assertIn("branches: [main]", source)
        for condition in (
            "github.event.workflow_run.conclusion == 'success'",
            "github.event.workflow_run.repository.full_name == github.repository",
            "github.event.workflow_run.head_repository.full_name == github.repository",
            "github.event.workflow_run.event == 'push'",
            "github.event.workflow_run.event == 'workflow_dispatch'",
        ):
            self.assertIn(condition, source)
        self.assertNotIn("build-cross-platform", source)
        self.assertNotIn("build-posix-github", source)
        self.assertNotIn("group: release-browser-${{ github.event.workflow_run.head_sha }}", source)
        self.assertIn("cancel-in-progress: false", source)

    def test_invalid_events_cannot_enter_validated_version_publish_queue(self):
        path = Path(__file__).resolve().parents[2] / ".github/workflows/release-browser.yml"
        source = path.read_text()
        defaults, jobs = source.split("\njobs:\n", 1)
        readiness, publishing = jobs.split("\n  release:\n", 1)
        self.assertIn("permissions:\n  actions: read\n  contents: read\n", defaults)
        self.assertNotIn("concurrency:", defaults)
        self.assertNotIn("contents: write", readiness)
        self.assertNotIn("concurrency:", readiness)
        self.assertIn("ready: ${{ steps.check.outputs.ready }}", readiness)
        self.assertIn("version: ${{ steps.check.outputs.version }}", readiness)
        self.assertIn("python3 tools/reconcile_browser_release.py --check-ready", readiness)
        self.assertIn("needs: readiness\n    if: needs.readiness.outputs.ready == 'true'", publishing)
        self.assertIn("    concurrency:\n      group: release-browser-publish-${{ needs.readiness.outputs.version }}\n"
                      "      cancel-in-progress: false", publishing)
        self.assertIn("RELEASE_VERSION: ${{ needs.readiness.outputs.version }}", publishing)
        self.assertNotIn("inputs.version", publishing)
        self.assertNotIn("github.event.workflow_run.head_sha", publishing)
        self.assertIn("contents: write", publishing)
        self.assertIn("run: python3 tools/reconcile_browser_release.py\n", publishing)
        self.assertIn("ref: main", readiness)
        self.assertIn("controller_sha: ${{ steps.controller.outputs.sha }}", readiness)
        self.assertIn("ref: ${{ needs.readiness.outputs.controller_sha }}", publishing)
        self.assertNotIn("ref: ${{ github.event.workflow_run.head_sha }}", source)
        for job in (readiness, publishing):
            self.assertIn("persist-credentials: false", job)
            self.assertIn("python-version: '3.13'", job)

    def test_listing_checks_sha_both_repositories_branch_event_name_and_path(self):
        invalid = [
            {"head_sha": OTHER_SHA}, {"head_sha": None},
            {"repository": {"full_name": "other/chromix"}},
            {"head_repository": {"full_name": "other/chromix"}},
            {"repository": None}, {"head_repository": None},
            {"head_branch": "feature"}, {"event": "pull_request"}, {"event": "workflow_call"},
            {"name": "build-cross-platform"},
            {"path": ".github/workflows/copied-workflow.yml"},
            {"path": ".github/workflows/build-linux-x64.yml@refs/heads/main"},
        ]
        valid = make_run(event="workflow_dispatch")
        self.pages = [{"workflow_runs": [make_run(run_id=1000 + i, **changes)
                                         for i, changes in enumerate(invalid)]},
                      {"workflow_runs": [valid]}]
        self.assertEqual(release.successful_runs(REPO, SHA), {valid["name"]: valid})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                release.validate_run(make_run(**changes), REPO, SHA)

    def test_newest_run_and_attempt_win_independent_of_page_order(self):
        old = make_run(run_id=100, run_attempt=9)
        latest = make_run(run_id=101, run_attempt=2)
        older_attempt = make_run(run_id=101, run_attempt=1)
        for pages in ([old, latest, older_attempt], [older_attempt, latest, old]):
            with self.subTest(order=[release.run_identity(run) for run in pages]):
                self.pages = [{"workflow_runs": [run]} for run in pages]
                self.assertEqual(release.successful_runs(REPO, SHA), {latest["name"]: latest})

    def test_newer_non_success_never_falls_back_to_old_success(self):
        for status, conclusion in (("completed", "failure"), ("completed", "cancelled"),
                                   ("completed", "skipped"), ("in_progress", None), ("queued", None)):
            for run_id, attempt in ((101, 1), (100, 2)):
                old = make_run()
                latest = make_run(run_id=run_id, run_attempt=attempt, status=status, conclusion=conclusion)
                for runs in ([old, latest], [latest, old]):
                    with self.subTest(status=status, conclusion=conclusion, run_id=run_id, attempt=attempt):
                        self.pages = [{"workflow_runs": [run]} for run in runs]
                        self.assertEqual(release.successful_runs(REPO, SHA), {})

    def test_conflicting_snapshot_of_same_attempt_fails_closed(self):
        success = make_run()
        pending = make_run(status="in_progress", conclusion=None)
        for runs in ([success, pending], [pending, success]):
            self.pages = [{"workflow_runs": [run]} for run in runs]
            self.assertEqual(release.successful_runs(REPO, SHA), {})


class BundleValidationTest(ReleaseFixtureTest):
    def test_manifest_accepts_named_assets_and_rejects_invalid_entries(self):
        self.assertEqual(release.parse_manifest(f"{'A' * 64} *chromix-linux-x64.zip\n"),
                         {"chromix-linux-x64.zip": "a" * 64})
        for text in (f"{'a' * 64}  unrelated.zip\n", "bad  chromix-linux-x64.zip\n",
                     f"{'a' * 64}  chromix-linux-x64.zip\n{'b' * 64}  chromix-linux-x64.zip\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                release.parse_manifest(text)

    def test_manifest_rejects_unsafe_paths_even_when_listed_as_existing_assets(self):
        for name in ("../LICENSE", "/LICENSE", "dir/LICENSE", "LICENSE\\outside", "LICENSE:stream", "SHA256SUMS"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.parse_manifest(f"{'a' * 64}  {name}\n", {name})

    def test_all_five_platform_layouts(self):
        for path in self.bundles().values():
            with self.subTest(asset=path.name):
                release.validate_bundle(path)

    def test_missing_or_empty_required_members_are_rejected(self):
        path = self.root / "chromix-win-x64.zip"
        write_bundle(path, missing="chromix/chrome.exe")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            release.validate_bundle(path)
        write_bundle(path, missing="chromix/chrome.exe", extra=("chromix/chrome.exe", ""))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            release.validate_bundle(path)

    def test_unsafe_and_duplicate_zip_members_are_rejected(self):
        path = self.root / "chromix-linux-x64.zip"
        for member in ("chromix/../outside", "/chromix/outside", "other/chrome",
                       "chromix/drive:file", "chromix\\outside", "chromix/chrome"):
            with self.subTest(member=member):
                write_bundle(path, extra=(member, "bad"))
                with self.assertRaises(ValueError):
                    release.validate_bundle(path)

    def test_corruption_is_rejected_even_when_checksum_matches(self):
        self.artifact_errors["chromix-linux-x64"] = "corrupt"
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            release.collect(REPO, self.runs["build-linux-x64"], self.root)
        self.assertEqual(self.mutations, [])

    def test_collection_downloads_only_each_workflows_owned_artifact(self):
        for name, run in self.runs.items():
            bundles = release.collect(REPO, run, self.root)
            self.assertEqual(set(bundles), {EXPECTED_WORKFLOWS[name][0] + ".zip"})
        self.assertEqual(self.downloads, [(run["id"], EXPECTED_WORKFLOWS[name][0])
                                         for name, run in self.runs.items()])


class ReadinessTest(ReleaseFixtureTest):
    def assert_read_only(self):
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.mutations, [])
        for call in self.gh.call_args_list:
            self.assertEqual(call.args[0], "api")
            self.assertTrue(any(f"repos/{REPO}/actions/runs" in arg for arg in call.args))

    def test_ready_mode_outputs_true_without_downloads_or_github_writes(self):
        self.pages = [{"workflow_runs": [self.event_run]}]
        (self.root / "output").write_text("existing=value\n")
        self.run_main("--check-ready")
        self.assertEqual((self.root / "output").read_text(), "existing=value\nready=true\n")
        self.assertEqual(self.gh.call_count, 2)
        self.assert_read_only()

    def test_other_platforms_missing_failed_running_or_foreign_sha_do_not_gate(self):
        for changes in (None, {"conclusion": "failure"}, {"status": "in_progress"}, {"head_sha": OTHER_SHA}):
            with self.subTest(changes=changes):
                runs = [self.event_run]
                if changes:
                    runs.append(make_run(**changes))
                self.pages = [{"workflow_runs": runs}]
                (self.root / "output").write_text("")
                self.run_main("--check-ready")
                self.assertEqual((self.root / "output").read_text(), "ready=true\n")
                self.assert_read_only()

    def test_missing_stale_or_failed_trigger_is_not_ready(self):
        for latest in (None, {"id": 200}, {"run_attempt": 2}, {"conclusion": "failure"},
                       {"status": "in_progress", "conclusion": None}):
            with self.subTest(latest=latest):
                runs = [{**self.event_run, **latest}] if latest else []
                self.pages = [{"workflow_runs": runs}]
                (self.root / "output").write_text("")
                self.run_main("--check-ready")
                self.assertEqual((self.root / "output").read_text(), "ready=false\n")
                self.assert_read_only()

    def test_rerun_success_is_not_consumed_by_previous_attempt_event(self):
        self.event_response = {**self.event_run, "run_attempt": 2}
        self.pages = [{"workflow_runs": [self.event_response]}]
        self.run_main("--check-ready")
        self.assertEqual((self.root / "output").read_text(), "ready=false\n")
        self.assert_read_only()

    def test_newer_same_platform_other_sha_does_not_block(self):
        self.pages = [{"workflow_runs": [self.event_run, {**self.event_run, "id": 200,
                                                        "head_sha": OTHER_SHA, "conclusion": "failure"}]}]
        self.run_main("--check-ready")
        self.assertEqual((self.root / "output").read_text(), "ready=true\n")
        self.assert_read_only()

    def test_invalid_trigger_does_not_emit_ready_output(self):
        self.event_response = {**self.event_run, "head_sha": OTHER_SHA}
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.run_main("--check-ready")
        self.assertFalse((self.root / "output").exists())
        self.assert_read_only()

    def test_publish_rechecks_readiness_after_waiting_for_global_queue(self):
        self.run_main("--check-ready")
        self.assertEqual((self.root / "output").read_text(), "ready=true\n")
        self.pages[0]["workflow_runs"].append({**self.event_run, "run_attempt": 2, "conclusion": "failure"})
        self.run_main()
        self.assertIn("Pending release", self.stdout.getvalue())
        self.assertEqual(self.gh.call_count, 4)
        self.assert_read_only()


class MainTest(ReleaseFixtureTest):
    def test_each_independent_platform_creates_a_draft_then_publishes_only_its_asset(self):
        for name, run in self.runs.items():
            with self.subTest(platform=name):
                self.event_run = self.event_response = run
                self.pages = [{"workflow_runs": [run]}]
                self.downloads.clear()
                self.mutations.clear()
                self.uploaded_files.clear()
                self.release = None
                self.release_files.clear()
                self.refs = []
                self.run_main()
                asset = EXPECTED_WORKFLOWS[name][0] + ".zip"
                self.assertEqual(self.downloads, [(run["id"], asset[:-4])])
                manifest_bytes = self.uploaded_files["SHA256SUMS"]
                backup = backup_name(manifest_bytes)
                self.assertEqual(set(self.uploaded_files), {asset, "SHA256SUMS", backup})
                self.assertEqual(self.release_files[backup], manifest_bytes)
                self.assertIn(backup, self.release_downloads)
                manifest = release.parse_manifest(manifest_bytes.decode())
                self.assertEqual(set(manifest), {asset})
                self.assertEqual(self.mutations[0][:2], ("release", "create"))
                self.assertIn("--draft", self.mutations[0])
                self.assertEqual(self.mutations[0][self.mutations[0].index("--target") + 1], SHA)
                self.assertEqual(self.mutations[0][self.mutations[0].index("--title") + 1], "Chromix 1.2.3.4")
                self.assertEqual([args[1] for args in self.mutations], ["create", "upload", "upload", "upload", "edit"])
                self.assertEqual([Path(args[3]).name for args in self.mutations if args[1] == "upload"],
                                 [asset, backup, "SHA256SUMS"])
                self.assertFalse(self.release["draft"])
                self.assertIn("--draft=false", self.mutations[-1])
                notes = self.notes[-1]
                self.assertEqual(notes.count("Verified build:"), 1)
                self.assertIn(f"Workflow: {name} (attempt 1)", notes)
                self.assertIn(f"Source commit: `{SHA}`", notes)
                self.assertIn(f"Assets: {asset}", notes)
                self.assertIn(run["html_url"], notes)
                self.assertNotIn("All five", notes)
                self.assertNotIn("only Windows", notes)

    def test_trigger_that_has_started_rerunning_is_pending(self):
        self.event_response = {**self.event_run, "status": "in_progress", "conclusion": None, "run_attempt": 2}
        self.run_main()
        self.assertIn("Pending release", self.stdout.getvalue())
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.mutations, [])

    def test_trigger_api_cannot_change_commit_workflow_path_id_or_event(self):
        for changes in ({"head_sha": OTHER_SHA}, {"name": "build-linux-x64"}, {"id": 999},
                        {"event": "workflow_dispatch"}, {"path": ".github/workflows/copied.yml"},
                        {"repository": {"full_name": "other/chromix"}},
                        {"head_repository": {"full_name": "other/chromix"}}, {"head_branch": "feature"}):
            with self.subTest(changes=changes):
                self.event_response = {**self.event_run, **changes}
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    self.run_main()
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.mutations, [])

    def test_old_aggregate_event_is_never_consumed_even_with_successful_platform_artifacts(self):
        for name in ("build-cross-platform", "build-posix-github"):
            for conclusion in ("failure", "success"):
                with self.subTest(name=name, conclusion=conclusion):
                    self.event_run = make_run(name, 34308090891, conclusion=conclusion)
                    with self.assertRaises(ValueError):
                        self.run_main()
        self.gh.assert_not_called()

    def test_invalid_incoming_artifact_blocks_all_mutations(self):
        for error in ("layout", "checksum", "missing-checksum", "foreign-checksum", "unexpected", "corrupt", "expired"):
            with self.subTest(error=error):
                self.downloads.clear()
                self.artifact_errors["chromix-win-x64"] = error
                with self.assertRaises((ValueError, zipfile.BadZipFile, RuntimeError)):
                    self.run_main()
                self.assertEqual(self.downloads, [(self.event_run["id"], "chromix-win-x64")])
                self.assertEqual(self.mutations, [])

    def test_run_changes_during_download_remain_pending(self):
        for changes in ({"run_attempt": 2, "status": "in_progress", "conclusion": None},
                        {"run_attempt": 2, "conclusion": "failure"}, {"run_attempt": 2}, {"id": 200}):
            with self.subTest(changes=changes):
                self.listing_responses = [self.pages, [{"workflow_runs": [{**self.event_run, **changes}]}]]
                self.run_main()
                self.assertEqual(self.mutations, [])
                self.assertIn("platform run changed", self.stdout.getvalue())

    def test_other_platform_changes_during_download_do_not_block(self):
        self.listing_responses = [self.pages, [{"workflow_runs": [self.event_run, make_run(conclusion="failure")]}]]
        self.run_main()
        self.assertEqual(self.mutations[-1][1], "edit")

    def test_invalid_version_blocks_collection_and_publication(self):
        self.version = "not-a-version"
        with self.assertRaisesRegex(ValueError, "Invalid Chromium version"):
            self.run_main()
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.mutations, [])

    def test_new_release_upload_failure_leaves_draft_not_partial_public_release(self):
        for asset in ("chromix-win-x64.zip", "SHA256SUMS"):
            with self.subTest(asset=asset):
                self.fail_upload = asset
                self.mutations.clear()
                self.release = None
                self.release_files.clear()
                self.refs = []
                with self.assertRaises(subprocess.CalledProcessError):
                    self.run_main()
                self.assertTrue(self.release["draft"])
                self.assertNotIn("SHA256SUMS", self.release_files)
                self.assertEqual(self.mutations[0][1], "create")
                self.assertIn("--draft", self.mutations[0])
                self.assertNotIn("edit", [args[1] for args in self.mutations])


class RevisionTest(ReleaseFixtureTest):
    def test_incoming_version_must_match_valid_tag_before_downloads_or_writes(self):
        bundles = self.incoming()
        for tag, version in ((TAG, "9.9.9.9"), ("v1.2.3.4-other", "1.2.3.4"), (TAG, "not-a-version")):
            with self.subTest(tag=tag, version=version):
                self.version = version
                with self.assertRaises(ValueError):
                    release.publish(REPO, self.event_run, tag, bundles, self.root)
                self.assertEqual(self.release_downloads, [])
                self.assertEqual(self.mutations, [])

    def test_existing_tag_can_point_to_other_sha_only_for_same_chromium_version(self):
        self.refs = [{"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": OTHER_SHA}}]
        self.assertEqual(release.validate_release_revision(REPO, TAG, SHA, None), OTHER_SHA)
        self.source_versions[OTHER_SHA] = "1.2.3.5"
        with self.assertRaisesRegex(ValueError, "Pinned tag commit Chromium version"):
            release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        self.assertEqual(self.release_downloads, [])
        self.assertEqual(self.mutations, [])

    def test_annotated_tags_are_peeled_and_version_checked(self):
        tag_sha = "c" * 40
        nested_sha = "d" * 40
        self.refs = [{"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": tag_sha}}]
        self.tags[tag_sha] = {"type": "tag", "sha": nested_sha}
        self.tags[nested_sha] = {"type": "commit", "sha": OTHER_SHA}
        self.assertEqual(release.validate_release_revision(REPO, TAG, SHA, None), OTHER_SHA)
        self.source_versions[OTHER_SHA] = "2.3.4.5"
        with self.assertRaisesRegex(ValueError, "Pinned tag commit Chromium version"):
            release.validate_release_revision(REPO, TAG, SHA, None)
        self.tags[nested_sha] = {"type": "tag", "sha": tag_sha}
        with self.assertRaisesRegex(ValueError, "Invalid annotated"):
            release.validate_release_revision(REPO, TAG, SHA, None)

    def test_noncommit_tag_is_rejected(self):
        self.refs = [{"ref": f"refs/tags/{TAG}", "object": {"type": "tree", "sha": SHA}}]
        with self.assertRaisesRegex(ValueError, "does not point to a commit"):
            release.validate_release_revision(REPO, TAG, SHA, None)

    def test_similar_prefix_tag_is_not_treated_as_release_tag(self):
        self.refs = [{"ref": f"refs/tags/{TAG}-other", "object": {"type": "commit", "sha": OTHER_SHA}}]
        self.assertEqual(release.validate_release_revision(REPO, TAG, SHA, None), SHA)

    def test_release_target_must_match_immutable_tag_not_incoming_sha(self):
        self.existing_release({}, sha=OTHER_SHA)
        self.assertEqual(release.validate_release_revision(REPO, TAG, SHA, self.release), OTHER_SHA)
        self.release["target_commitish"] = SHA
        with self.assertRaisesRegex(ValueError, "target conflicts"):
            release.validate_release_revision(REPO, TAG, SHA, self.release)
        self.release["target_commitish"] = "main"
        self.assertEqual(release.validate_release_revision(REPO, TAG, SHA, self.release), OTHER_SHA)

    def test_published_release_requires_tag_and_untagged_draft_requires_commit_version(self):
        self.existing_release({}, sha=OTHER_SHA)
        self.refs = []
        with self.assertRaisesRegex(ValueError, "no verifiable tag or draft commit"):
            release.validate_release_revision(REPO, TAG, SHA, self.release)
        self.release["draft"] = True
        self.assertEqual(release.validate_release_revision(REPO, TAG, SHA, self.release), OTHER_SHA)
        self.release["target_commitish"] = "main"
        with self.assertRaisesRegex(ValueError, "no verifiable tag or draft commit"):
            release.validate_release_revision(REPO, TAG, SHA, self.release)
        self.release["target_commitish"] = OTHER_SHA
        self.source_versions[OTHER_SHA] = "1.2.3.5"
        with self.assertRaisesRegex(ValueError, "Pinned tag commit Chromium version"):
            release.validate_release_revision(REPO, TAG, SHA, self.release)

    def test_new_release_uses_existing_tag_commit_without_moving_tag(self):
        self.refs = [{"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": OTHER_SHA}}]
        release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        create = self.mutations[0]
        self.assertEqual(create[create.index("--target") + 1], OTHER_SHA)
        self.assertTrue(all(args[0] == "release" for args in self.mutations))
        self.assertNotIn("--target", self.mutations[-1])

    def test_changed_pinned_commit_during_download_fails_before_mutation(self):
        self.existing_release({}, sha=OTHER_SHA)
        self.release["target_commitish"] = "main"
        original_gh = self.fake_gh

        def download_then_move_tag(*args):
            result = original_gh(*args)
            if args[:2] == ("release", "download"):
                self.refs[0]["object"]["sha"] = "c" * 40
            return result

        self.gh.side_effect = download_then_move_tag
        with self.assertRaisesRegex(ValueError, "Pinned release commit changed"):
            release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        self.assertEqual(self.mutations, [])


class PublicationTest(ReleaseFixtureTest):
    def test_publishing_accepts_only_triggering_platform_asset(self):
        for bundles in ({}, self.bundles(), {"chromix-win-x64.zip": self.root / "chromix-mac-x64.zip"}):
            with self.subTest(assets=set(bundles)), self.assertRaisesRegex(ValueError, "triggering platform"):
                release.publish(REPO, self.event_run, TAG, bundles, self.root)
        self.gh.assert_not_called()

    def test_append_to_different_sha_release_preserves_all_assets_hashes_and_provenance(self):
        bundles = self.bundles()
        incoming = {"chromix-win-x64.zip": bundles.pop("chromix-win-x64.zip")}
        sidecar = self.root / "LICENSE.chromium"
        sidecar.write_text("license fixture")
        bundles[sidecar.name] = sidecar
        self.existing_release(bundles, sha=OTHER_SHA)
        self.release_files["unlisted.txt"] = b"untouched metadata"
        self.release["assets"].append({"name": "unlisted.txt"})
        self.release["body"] += f"Source commit: `{'c' * 40}`\n"
        original = copy.deepcopy(self.release_files)
        original_notes = self.release["body"]
        original_refs = copy.deepcopy(self.refs)
        release.publish(REPO, self.event_run, TAG, incoming, self.root)
        backups = {backup_name(original["SHA256SUMS"]), backup_name(self.uploaded_files["SHA256SUMS"])}
        self.assertEqual(set(self.uploaded_files), {"chromix-win-x64.zip", "SHA256SUMS"} | backups)
        for name, data in original.items():
            if name != "SHA256SUMS":
                self.assertEqual(self.release_files[name], data)
        self.assertEqual(self.release_files[backup_name(original["SHA256SUMS"])], original["SHA256SUMS"])
        self.assertEqual(self.release_files[backup_name(self.uploaded_files["SHA256SUMS"])],
                         self.uploaded_files["SHA256SUMS"])
        self.assertEqual(self.refs, original_refs)
        self.assertTrue(self.uploaded_files["SHA256SUMS"].startswith(original["SHA256SUMS"]))
        merged = release.parse_manifest(self.uploaded_files["SHA256SUMS"].decode(), set(original) | release.ASSETS)
        for name, checksum in release.parse_manifest(original["SHA256SUMS"].decode(), set(original)).items():
            self.assertEqual(merged[name], checksum)
        self.assertEqual(set(merged), set(bundles) | set(incoming))
        notes = self.notes[-1]
        self.assertTrue(notes.startswith(original_notes))
        self.assertIn(f"Source commit: `{SHA}`", notes)
        self.assertIn("may use different source commits", notes)
        self.assertNotIn("All five platforms", notes)
        self.assertEqual(set(self.release_downloads), set(bundles) | {"SHA256SUMS"} | backups)
        self.assertTrue(all("--target" not in args for args in self.mutations))
        self.assertTrue(all("--clobber" not in args for args in self.mutations if args[1] == "upload"
                            and Path(args[3]).name != "SHA256SUMS"))

    def test_four_later_platforms_append_to_manual_windows_release_at_distinct_source_shas(self):
        windows = self.incoming()["chromix-win-x64.zip"]
        write_bundle(windows, missing="chromix/LICENSE.chromium")
        initial = {windows.name: windows}
        for name in ("LICENSE.chromix", "LICENSE.chromium"):
            path = self.root / name
            path.write_text(f"sidecar {name}")
            initial[name] = path
        self.existing_release(initial, sha=OTHER_SHA)
        pinned_refs = copy.deepcopy(self.refs)
        for index, name in enumerate(list(EXPECTED_WORKFLOWS)[:-1]):
            with self.subTest(platform=name):
                sha = str(index + 1) * 40
                self.event_run = self.event_response = {**self.runs[name], "head_sha": sha}
                self.pages = [{"workflow_runs": [self.event_run]}]
                before = copy.deepcopy(self.release_files)
                self.uploaded_files.clear()
                self.mutations.clear()
                root = self.root / name
                root.mkdir()
                release.publish(REPO, self.event_run, TAG, self.incoming(), root)
                asset = EXPECTED_WORKFLOWS[name][0] + ".zip"
                backups = {backup_name(before["SHA256SUMS"]), backup_name(self.uploaded_files["SHA256SUMS"])}
                new_backups = backups - set(before)
                self.assertEqual(set(self.uploaded_files), {asset, "SHA256SUMS"} | new_backups)
                self.assertTrue(self.uploaded_files["SHA256SUMS"].startswith(before["SHA256SUMS"]))
                for backup in backups:
                    self.assertIn(backup, self.release_downloads)
                for key, value in before.items():
                    if key != "SHA256SUMS":
                        self.assertEqual(self.release_files[key], value)
                self.assertEqual(self.refs, pinned_refs)
                self.assertIn(f"Source commit: `{sha}`", self.release["body"])
                self.assertIn(f"Workflow: {name} (attempt 1)", self.release["body"])
                self.assertEqual([args[1] for args in self.mutations],
                                 ["upload"] * len(new_backups) + ["edit", "upload", "upload", "edit"])
                edits = [args for args in self.mutations if args[1] == "edit"]
                self.assertNotIn("--draft=false", edits[0])
                self.assertIn("--draft=false", edits[-1])
        backup_assets = {name for name in self.release_files if name.startswith("SHA256SUMS.backup.")}
        self.assertEqual(len(backup_assets), 5)
        for name in backup_assets:
            self.assertEqual(name, backup_name(self.release_files[name]))
        self.assertEqual(set(self.release_files),
                         release.ASSETS | {"SHA256SUMS", "LICENSE.chromix", "LICENSE.chromium"} | backup_assets)
        self.assertIn(f"Source commit: `{OTHER_SHA}`", self.release["body"])
        self.assertEqual(self.release["body"].count("Verified build:"), 4)

    def test_legacy_windows_zip_without_internal_licenses_is_not_repacked_or_revalidated(self):
        bundles = self.bundles()
        windows = bundles["chromix-win-x64.zip"]
        write_bundle(windows, missing="chromix/LICENSE.chromium")
        license_path = self.root / "LICENSE.chromium"
        license_path.write_text("sidecar license")
        self.existing_release({windows.name: windows, license_path.name: license_path}, sha=OTHER_SHA)
        self.event_run = self.event_response = self.runs["build-linux-x64"]
        original = windows.read_bytes()
        release.publish(REPO, self.event_run, TAG, {"chromix-linux-x64.zip": bundles["chromix-linux-x64.zip"]}, self.root)
        self.assertNotIn(windows.name, self.uploaded_files)
        self.assertEqual(self.release_files[windows.name], original)
        self.assertIn(license_path.name.encode(), self.uploaded_files["SHA256SUMS"])

    def test_identical_retry_preserves_provenance_and_does_not_clobber_manifest(self):
        incoming = self.incoming()
        self.existing_release(incoming)
        release.publish(REPO, self.event_run, TAG, incoming, self.root)
        notes = self.notes[-1]
        self.assertEqual(notes.count("Verified build:"), 1)
        self.release["body"] = notes
        second_root = self.root / "retry"
        second_root.mkdir()
        release.publish(REPO, self.event_run, TAG, incoming, second_root)
        self.assertEqual(self.notes[-1], notes)
        self.assertEqual([args[1] for args in self.mutations], ["edit", "edit"])
        self.assertEqual(self.uploaded_files, {})

    def test_legacy_same_source_claim_is_removed_without_removing_provenance(self):
        self.existing_release({})
        self.release["body"] += "All five platforms are verified at one source commit. Old provenance."
        release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        self.assertNotIn("All five platforms", self.notes[-1])
        self.assertIn("Old provenance.", self.notes[-1])
        self.assertIn("may use different source commits", self.notes[-1])

    def test_different_published_bytes_are_never_replaced_even_same_version(self):
        bundles = self.incoming()
        self.existing_release(bundles, sha=OTHER_SHA)
        write_bundle(bundles["chromix-win-x64.zip"], extra=("chromix/extra", "different"))
        with self.assertRaisesRegex(ValueError, "Refusing to replace a different published browser"):
            release.publish(REPO, self.event_run, TAG, bundles, self.root)
        self.assertEqual(self.mutations, [])

    def test_existing_manifest_mismatch_fails_before_any_mutation(self):
        bundles = self.incoming()
        self.existing_release(bundles)
        self.release_files["SHA256SUMS"] = f"{'0' * 64}  chromix-win-x64.zip\n".encode("ascii")
        with self.assertRaisesRegex(ValueError, "Existing release checksum mismatch"):
            release.publish(REPO, self.event_run, TAG, bundles, self.root)
        self.assertEqual(self.mutations, [])

    def test_sidecar_checksum_mismatch_is_not_silently_rewritten(self):
        sidecar = self.root / "LICENSE.chromix"
        sidecar.write_text("original license")
        self.existing_release({sidecar.name: sidecar})
        self.release_files[sidecar.name] = b"changed license"
        with self.assertRaisesRegex(ValueError, "Existing release checksum mismatch"):
            release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        self.assertEqual(self.mutations, [])

    def test_unrelated_public_browser_without_existing_checksum_is_not_adopted(self):
        bundles = self.bundles()
        self.existing_release({"chromix-linux-x64.zip": bundles["chromix-linux-x64.zip"]})
        self.release_files["SHA256SUMS"] = b""
        with self.assertRaisesRegex(ValueError, "missing existing checksums"):
            release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.mutations, [])

    def test_matching_incoming_public_browser_can_recover_missing_checksum(self):
        incoming = self.incoming()
        self.existing_release(incoming)
        original = self.release_files["chromix-win-x64.zip"]
        self.release_files["SHA256SUMS"] = b""
        release.publish(REPO, self.event_run, TAG, incoming, self.root)
        hashes = release.parse_manifest(self.uploaded_files["SHA256SUMS"].decode())
        self.assertEqual(hashes["chromix-win-x64.zip"], release.digest(incoming["chromix-win-x64.zip"]))
        self.assertEqual(self.downloads, [(self.event_run["id"], "chromix-win-x64")])
        self.assertEqual(self.release_files["chromix-win-x64.zip"], original)
        self.assertEqual(set(self.uploaded_files), {"SHA256SUMS", backup_name(b""),
                                                  backup_name(self.uploaded_files["SHA256SUMS"])})

    def test_recovered_slot_must_match_fresh_artifact_not_just_local_bytes(self):
        incoming = self.incoming()
        write_bundle(incoming["chromix-win-x64.zip"], extra=("chromix/extra", "untrusted"))
        self.existing_release(incoming)
        self.release_files["SHA256SUMS"] = b""
        with self.assertRaisesRegex(ValueError, "differs from verified incoming artifact"):
            release.publish(REPO, self.event_run, TAG, incoming, self.root)
        self.assertEqual(self.downloads, [(self.event_run["id"], "chromix-win-x64")])
        self.assertEqual(self.mutations, [])

    def test_orphan_recollection_keeps_artifact_validation_fail_closed(self):
        incoming = self.incoming()
        self.existing_release(incoming)
        self.release_files["SHA256SUMS"] = b""
        for error in ("layout", "checksum", "missing-checksum", "foreign-checksum", "unexpected", "corrupt", "expired"):
            with self.subTest(error=error):
                root = self.root / error
                root.mkdir()
                self.downloads.clear()
                self.artifact_errors["chromix-win-x64"] = error
                with self.assertRaises((ValueError, zipfile.BadZipFile, RuntimeError)):
                    release.publish(REPO, self.event_run, TAG, incoming, root)
                self.assertEqual(self.downloads, [(self.event_run["id"], "chromix-win-x64")])
                self.assertEqual(self.mutations, [])

    def test_rerun_during_existing_asset_download_blocks_all_mutations(self):
        self.existing_release(self.incoming())
        original_gh = self.fake_gh

        def download_then_rerun(*args):
            result = original_gh(*args)
            if args[:2] == ("release", "download"):
                self.event_response = {**self.event_run, "run_attempt": 2, "status": "in_progress", "conclusion": None}
            return result

        self.gh.side_effect = download_then_rerun
        release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        self.assertIn("Pending release", self.stdout.getvalue())
        self.assertEqual(self.mutations, [])

    def test_existing_manifest_cannot_reference_missing_assets(self):
        self.existing_release(self.incoming())
        self.release["assets"] = [{"name": "SHA256SUMS"}]
        with self.assertRaisesRegex(ValueError, "references missing release assets"):
            release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        self.assertEqual(self.mutations, [])

    def test_duplicate_or_unsafe_existing_manifest_entries_fail_closed(self):
        self.existing_release(self.incoming())
        self.release_files["SHA256SUMS"] *= 2
        with self.assertRaisesRegex(ValueError, "Invalid or duplicate"):
            release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        self.assertEqual(self.mutations, [])

    def test_duplicate_release_asset_names_fail_closed(self):
        self.existing_release(self.incoming())
        self.release["assets"].append({"name": "chromix-win-x64.zip"})
        with self.assertRaisesRegex(ValueError, "Duplicate existing release asset"):
            release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        self.assertEqual(self.mutations, [])

    def test_existing_manifest_bytes_are_preserved_including_crlf_uppercase_and_no_newline(self):
        bundles = self.bundles()
        self.existing_release({"chromix-linux-x64.zip": bundles["chromix-linux-x64.zip"]})
        checksum = release.digest(bundles["chromix-linux-x64.zip"]).upper()
        original = f"\r\n{checksum} *chromix-linux-x64.zip".encode()
        self.release_files["SHA256SUMS"] = original
        release.publish(REPO, self.event_run, TAG, self.incoming(), self.root)
        self.assertTrue(self.uploaded_files["SHA256SUMS"].startswith(original + b"\n"))
        self.assertEqual(len(release.parse_manifest(self.uploaded_files["SHA256SUMS"].decode())), 2)

    def test_manifest_upload_failure_restores_old_manifest_and_retry_recovers_remote_zip(self):
        self.existing_release({})
        original = self.release_files["SHA256SUMS"]
        incoming = self.incoming()
        self.fail_upload = "SHA256SUMS"
        with self.assertRaises(subprocess.CalledProcessError):
            release.publish(REPO, self.event_run, TAG, incoming, self.root)
        self.assertEqual(self.uploaded_files["SHA256SUMS"], original)
        self.assertEqual(self.release_files["SHA256SUMS"], original)
        self.assertEqual(self.release_files["chromix-win-x64.zip"], incoming["chromix-win-x64.zip"].read_bytes())
        self.assertEqual([args[1] for args in self.mutations], ["upload", "upload", "edit", "upload", "upload", "upload"])
        self.assertNotIn("--draft=false", self.mutations[2])
        self.assertIn(f"Source commit: `{SHA}`", self.release["body"])
        self.assertIn("existing", self.mutations[-1][3])
        backups = {name: data for name, data in self.release_files.items() if name.startswith("SHA256SUMS.backup.")}
        self.assertEqual(len(backups), 2)
        self.assertEqual(backups[backup_name(original)], original)
        retry = self.root / "retry"
        retry.mkdir()
        release.publish(REPO, self.event_run, TAG, incoming, retry)
        self.assertEqual(self.downloads, [(self.event_run["id"], "chromix-win-x64")])
        self.assertEqual(len([args for args in self.mutations if args[1] == "upload"
                              and Path(args[3]).name == "chromix-win-x64.zip"]), 1)
        hashes = release.parse_manifest(self.release_files["SHA256SUMS"].decode())
        self.assertEqual(hashes, {"chromix-win-x64.zip": release.digest(incoming["chromix-win-x64.zip"])})
        for name, data in backups.items():
            self.assertEqual(self.release_files[name], data)
        self.assertEqual(self.mutations[-1][1], "edit")

    def test_partial_draft_is_completed_without_waiting_for_other_platforms(self):
        incoming = self.incoming()
        self.existing_release(incoming, draft=True, sha=OTHER_SHA)
        self.refs = []
        self.release_files.pop("SHA256SUMS")
        self.release["assets"] = [{"name": "chromix-win-x64.zip"}]
        release.publish(REPO, self.event_run, TAG, incoming, self.root)
        self.assertEqual(set(self.uploaded_files), {"SHA256SUMS", backup_name(self.uploaded_files["SHA256SUMS"])})
        self.assertEqual(self.downloads, [(self.event_run["id"], "chromix-win-x64")])
        self.assertFalse(self.release["draft"])
        self.assertEqual(self.mutations[-1][1], "edit")
        self.assertIn("--draft=false", self.mutations[-1])
        self.assertNotIn("--target", self.mutations[-1])


if __name__ == "__main__":
    unittest.main()
