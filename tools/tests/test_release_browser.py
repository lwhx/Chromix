"""Release orchestration tests use tiny ZIP fixtures and a mocked GitHub CLI."""
import copy
import io
import json
import os
import re
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
                    archive.writestr(name, "fixture")
            if extra:
                archive.writestr(*extra)
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
        self.event_run = self.runs["build-win-x64-github"]
        self.event_response = self.event_run
        self.listing_responses = []
        self.gh = self.enterContext(patch.object(release, "gh", side_effect=self.fake_gh))
        self.stdout = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))

    def fake_gh(self, *args):
        if args[:3] == ("api", "--paginate", "--slurp"):
            if args[3] == f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100":
                pages = self.listing_responses.pop(0) if self.listing_responses else self.pages
                return json.dumps(pages)
            if args[3] == f"repos/{REPO}/releases?per_page=100":
                return json.dumps([[self.release] if self.release else []])
        if args[0] == "api":
            if args[1] == f"repos/{REPO}/actions/runs/{self.event_run['id']}":
                return json.dumps(self.event_response)
            if args[1] == f"repos/{REPO}/contents/CHROMIUM_VERSION?ref={SHA}":
                return self.version
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
            if error == "unexpected":
                (dest / "unrelated.txt").write_text("unexpected")
            return ""
        if args[:2] == ("release", "download"):
            name = args[args.index("--pattern") + 1]
            dest = Path(args[args.index("--dir") + 1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / name).write_bytes(self.release_files[name])
            return ""
        if args[:2] in (("release", "create"), ("release", "upload"), ("release", "edit")):
            self.mutations.append(args)
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

    def existing_release(self, bundles, draft=False):
        self.refs = [{"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": SHA}}]
        self.release_files = {name: path.read_bytes() for name, path in bundles.items()}
        self.release_files["SHA256SUMS"] = "".join(
            f"{release.digest(path)}  {name}\n" for name, path in sorted(bundles.items())
        ).encode("ascii")
        self.release = {
            "tag_name": TAG, "target_commitish": SHA, "draft": draft,
            "body": f"Existing release\nSource commit: `{SHA}`\n",
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
        self.assertIn("group: release-browser-${{ github.event.workflow_run.head_sha }}", source)
        self.assertIn("cancel-in-progress: false", source)

    def test_incomplete_events_cannot_enter_global_publish_queue(self):
        path = Path(__file__).resolve().parents[2] / ".github/workflows/release-browser.yml"
        source = path.read_text()
        defaults, jobs = source.split("\njobs:\n", 1)
        readiness, publishing = jobs.split("\n  release:\n", 1)
        self.assertIn("permissions:\n  actions: read\n  contents: read\n", defaults)
        self.assertIn("group: release-browser-${{ github.event.workflow_run.head_sha }}", defaults)
        self.assertNotIn("contents: write", readiness)
        self.assertNotIn("concurrency:", readiness)
        self.assertIn("  readiness:\n", readiness)
        self.assertIn("ready: ${{ steps.check.outputs.ready }}", readiness)
        self.assertIn("id: check", readiness)
        self.assertIn("python3 tools/release_browser.py --check-ready", readiness)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", readiness)
        self.assertIn("needs: readiness\n    if: needs.readiness.outputs.ready == 'true'", publishing)
        self.assertIn("    concurrency:\n      group: release-browser-publish\n      cancel-in-progress: false", publishing)
        self.assertIn("contents: write", publishing)
        self.assertIn("run: python3 tools/release_browser.py\n", publishing)
        self.assertNotIn("--check-ready", publishing)
        for job in (readiness, publishing):
            self.assertIn("uses: actions/checkout@v4", job)
            self.assertIn("ref: ${{ github.event.workflow_run.head_sha }}", job)
            self.assertIn("persist-credentials: false", job)
            self.assertIn("uses: actions/setup-python@v5", job)
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

    def test_validation_rejects_mixed_sha_or_workflow_mapping(self):
        self.assertEqual(release.validate_runs(self.runs, REPO), SHA)
        for changes in ({"head_sha": OTHER_SHA}, {"conclusion": "failure"}, {"status": "in_progress"}):
            runs = copy.deepcopy(self.runs)
            runs["build-linux-arm64"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                release.validate_runs(runs, REPO)
        runs = dict(self.runs)
        runs["build-linux-x64"] = runs["build-macos-x64"]
        with self.assertRaises(ValueError):
            release.validate_runs(runs, REPO)


class BundleValidationTest(ReleaseFixtureTest):
    def test_manifest_accepts_named_assets_and_rejects_invalid_entries(self):
        self.assertEqual(release.parse_manifest(f"{'A' * 64} *chromix-linux-x64.zip\n"),
                         {"chromix-linux-x64.zip": "a" * 64})
        for text in (f"{'a' * 64}  unrelated.zip\n", "bad  chromix-linux-x64.zip\n",
                     f"{'a' * 64}  chromix-linux-x64.zip\n{'b' * 64}  chromix-linux-x64.zip\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                release.parse_manifest(text)

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
        (self.root / "output").write_text("existing=value\n")
        self.run_main("--check-ready")
        self.assertEqual((self.root / "output").read_text(), "existing=value\nready=true\n")
        self.assertEqual(self.gh.call_count, 2)
        self.assert_read_only()

    def test_missing_failed_running_or_foreign_sha_platform_outputs_false(self):
        for state in ("missing", "failure", "in_progress", "other-sha", "trigger-rerun"):
            with self.subTest(state=state):
                self.pages = [{"workflow_runs": copy.deepcopy(list(self.runs.values()))}]
                self.event_response = self.event_run
                if state == "missing":
                    self.pages[0]["workflow_runs"].pop(0)
                elif state == "other-sha":
                    self.pages[0]["workflow_runs"][0]["head_sha"] = OTHER_SHA
                elif state == "trigger-rerun":
                    self.event_response = {**self.event_run, "status": "in_progress", "conclusion": None}
                else:
                    self.pages[0]["workflow_runs"].append(make_run(
                        run_id=100, run_attempt=2, status="completed" if state == "failure" else state,
                        conclusion="failure" if state == "failure" else None))
                (self.root / "output").write_text("")
                self.run_main("--check-ready")
                self.assertEqual((self.root / "output").read_text(), "ready=false\n")
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
        self.pages[0]["workflow_runs"].append(make_run(run_id=100, run_attempt=2, conclusion="failure"))
        self.run_main()
        self.assertIn("Pending release", self.stdout.getvalue())
        self.assertEqual(self.gh.call_count, 4)
        self.assert_read_only()


class MainTest(ReleaseFixtureTest):
    def test_incomplete_set_is_normal_pending_without_download_or_publish(self):
        self.pages[0]["workflow_runs"] = list(self.runs.values())[:-1]
        self.run_main()
        self.assertIn("Pending release", self.stdout.getvalue())
        self.assertIn("build-win-x64-github", self.stdout.getvalue())
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.mutations, [])
        self.assertFalse(any("contents/CHROMIUM_VERSION" in str(call) for call in self.gh.call_args_list))

    def test_old_trigger_is_pending_when_latest_platform_attempt_failed(self):
        self.pages[0]["workflow_runs"].append(make_run(run_id=100, run_attempt=2, conclusion="failure"))
        self.run_main()
        self.assertIn("Pending release", self.stdout.getvalue())
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.mutations, [])

    def test_trigger_that_has_started_rerunning_is_pending(self):
        self.event_response = {**self.event_run, "status": "in_progress", "conclusion": None, "run_attempt": 2}
        self.run_main()
        self.assertIn("Pending release", self.stdout.getvalue())
        self.assertEqual(self.downloads, [])

    def test_trigger_api_cannot_change_commit_or_workflow(self):
        for changes in ({"head_sha": OTHER_SHA}, {"name": "build-linux-x64"},
                        {"repository": {"full_name": "other/chromix"}}):
            with self.subTest(changes=changes):
                self.event_response = {**self.event_run, **changes}
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    self.run_main()
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.mutations, [])

    def test_any_invalid_artifact_blocks_all_publication_including_bad_fifth(self):
        for error in ("layout", "checksum", "missing-checksum", "unexpected", "corrupt", "expired"):
            with self.subTest(error=error):
                self.downloads.clear()
                self.artifact_errors["chromix-win-x64"] = error
                with self.assertRaises((ValueError, zipfile.BadZipFile, RuntimeError)):
                    self.run_main()
                self.assertEqual(len(self.downloads), 5)
                self.assertEqual(self.mutations, [])

    def test_run_changes_during_download_remain_pending(self):
        for status, conclusion in (("in_progress", None), ("completed", "failure"), ("completed", "success")):
            with self.subTest(status=status, conclusion=conclusion):
                current = copy.deepcopy(self.pages)
                current[0]["workflow_runs"][0].update(run_attempt=2, status=status, conclusion=conclusion)
                self.listing_responses = [self.pages, current]
                self.run_main()
                self.assertEqual(self.mutations, [])
                self.assertIn("platform runs changed", self.stdout.getvalue())

    def test_last_completed_event_publishes_only_after_all_five_validate(self):
        real_publish = release.publish

        def checked_publish(repo, runs, tag, bundles, root):
            self.assertEqual(len(self.downloads), 5)
            self.assertEqual(set(bundles), release.ASSETS)
            self.assertEqual(self.mutations, [])
            for path in bundles.values():
                release.validate_bundle(path)
            real_publish(repo, runs, tag, bundles, root)
            notes = (root / "notes.md").read_text()
            self.assertEqual(notes.count(f"Source commit: `{SHA}`"), 5)
            for name, run in self.runs.items():
                self.assertIn(run["html_url"], notes)
                self.assertIn(f"Workflow: {name} (attempt 1)", notes)
                self.assertIn(f"Assets: {EXPECTED_WORKFLOWS[name][0]}.zip", notes)
            self.assertEqual(set(release.parse_manifest((root / "SHA256SUMS").read_text())), release.ASSETS)

        with patch.object(release, "publish", side_effect=checked_publish) as publish:
            self.run_main()
        publish.assert_called_once()
        self.assertEqual(self.mutations[0][:2], ("release", "create"))
        self.assertIn("--draft", self.mutations[0])
        self.assertEqual(self.mutations[0][self.mutations[0].index("--target") + 1], SHA)
        self.assertEqual(len([args for args in self.mutations if args[:2] == ("release", "upload")]), 6)
        self.assertEqual(self.mutations[-1][:2], ("release", "edit"))
        self.assertIn("--draft=false", self.mutations[-1])

    def test_invalid_version_blocks_collection_and_publication(self):
        self.version = "not-a-version"
        with self.assertRaisesRegex(ValueError, "Invalid Chromium version"):
            self.run_main()
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.mutations, [])


class PublicationTest(ReleaseFixtureTest):
    def test_publishing_requires_complete_same_sha_runs_and_assets(self):
        bundles = self.bundles()
        incomplete = dict(bundles)
        incomplete.pop("chromix-win-x64.zip")
        with self.assertRaisesRegex(ValueError, "All five verified"):
            release.publish(REPO, self.runs, TAG, incomplete, self.root)
        self.runs["build-macos-arm64"]["head_sha"] = OTHER_SHA
        with self.assertRaises(ValueError):
            release.publish(REPO, self.runs, TAG, bundles, self.root)
        self.gh.assert_not_called()

    def test_existing_tag_must_resolve_to_built_sha_even_without_release(self):
        bundles = self.bundles()
        self.refs = [{"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": OTHER_SHA}}]
        with self.assertRaisesRegex(ValueError, "does not point to the built commit"):
            release.publish(REPO, self.runs, TAG, bundles, self.root)
        self.assertEqual(self.mutations, [])

    def test_annotated_tags_are_peeled_and_checked(self):
        tag_sha = "c" * 40
        nested_sha = "d" * 40
        self.refs = [{"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": tag_sha}}]
        self.tags[tag_sha] = {"type": "tag", "sha": nested_sha}
        self.tags[nested_sha] = {"type": "commit", "sha": SHA}
        release.validate_release_revision(REPO, TAG, SHA, None)
        self.tags[nested_sha]["sha"] = OTHER_SHA
        with self.assertRaisesRegex(ValueError, "does not point"):
            release.validate_release_revision(REPO, TAG, SHA, None)
        self.tags[nested_sha] = {"type": "tag", "sha": tag_sha}
        with self.assertRaisesRegex(ValueError, "Invalid annotated"):
            release.validate_release_revision(REPO, TAG, SHA, None)

    def test_similar_prefix_tag_is_not_treated_as_release_tag(self):
        self.refs = [{"ref": f"refs/tags/{TAG}-other", "object": {"type": "commit", "sha": OTHER_SHA}}]
        release.validate_release_revision(REPO, TAG, SHA, None)

    def test_existing_release_commit_and_provenance_cannot_conflict(self):
        bundles = self.bundles()
        for mismatch in ("tag", "target", "provenance", "missing-tag"):
            with self.subTest(mismatch=mismatch):
                self.existing_release(bundles)
                if mismatch == "tag":
                    self.refs[0]["object"]["sha"] = OTHER_SHA
                elif mismatch == "target":
                    self.release["target_commitish"] = OTHER_SHA
                elif mismatch == "provenance":
                    self.release["body"] = f"Source commit: `{OTHER_SHA}`"
                else:
                    self.refs = []
                with self.assertRaises(ValueError):
                    release.publish(REPO, self.runs, TAG, bundles, self.root)
                self.assertEqual(self.mutations, [])
                self.assertFalse((self.root / "existing").exists())

    def test_published_branch_target_uses_immutable_tag_not_branch_tip(self):
        bundles = self.bundles()
        self.existing_release(bundles)
        self.release["target_commitish"] = "main"
        release.publish(REPO, self.runs, TAG, bundles, self.root)
        self.assertEqual([args[1] for args in self.mutations], ["upload", "edit"])
        self.assertEqual(Path(self.mutations[0][3]).name, "SHA256SUMS")

    def test_unpublished_draft_without_tag_requires_exact_commit(self):
        bundles = self.bundles()
        self.existing_release(bundles, draft=True)
        self.refs = []
        self.release["target_commitish"] = "main"
        with self.assertRaisesRegex(ValueError, "no verifiable tag or draft commit"):
            release.publish(REPO, self.runs, TAG, bundles, self.root)
        self.release["target_commitish"] = SHA
        release.publish(REPO, self.runs, TAG, bundles, self.root)
        self.assertEqual([args[1] for args in self.mutations], ["upload", "edit"])

    def test_same_revision_retry_preserves_all_provenance_without_duplicates(self):
        bundles = self.bundles()
        self.existing_release(bundles)
        release.publish(REPO, self.runs, TAG, bundles, self.root)
        notes = (self.root / "notes.md").read_text()
        self.assertEqual(notes.count("Verified build:"), 5)
        self.release["body"] = notes
        second_root = self.root / "retry"
        second_root.mkdir()
        release.publish(REPO, self.runs, TAG, bundles, second_root)
        self.assertEqual((second_root / "notes.md").read_text(), notes)
        self.assertTrue(all(args[1] != "create" for args in self.mutations))
        self.assertTrue(all(Path(args[3]).name == "SHA256SUMS"
                            for args in self.mutations if args[1] == "upload"))

    def test_different_published_bytes_are_never_replaced(self):
        bundles = self.bundles()
        self.existing_release(bundles)
        write_bundle(bundles["chromix-win-x64.zip"], extra=("chromix/extra", "different"))
        with self.assertRaisesRegex(ValueError, "Refusing to replace a different published browser"):
            release.publish(REPO, self.runs, TAG, bundles, self.root)
        self.assertEqual(self.mutations, [])

    def test_existing_manifest_mismatch_fails_before_any_mutation(self):
        bundles = self.bundles()
        self.existing_release(bundles)
        self.release_files["SHA256SUMS"] = f"{'0' * 64}  chromix-linux-x64.zip\n".encode("ascii")
        with self.assertRaisesRegex(ValueError, "Existing release checksum mismatch"):
            release.publish(REPO, self.runs, TAG, bundles, self.root)
        self.assertEqual(self.mutations, [])

    def test_rerun_during_existing_asset_download_blocks_all_mutations(self):
        bundles = self.bundles()
        self.existing_release(bundles)
        original_gh = self.fake_gh

        def download_then_rerun(*args):
            result = original_gh(*args)
            if args[:2] == ("release", "download"):
                self.pages = copy.deepcopy(self.pages)
                self.pages[0]["workflow_runs"][0].update(run_attempt=2, status="in_progress", conclusion=None)
            return result

        self.gh.side_effect = download_then_rerun
        release.publish(REPO, self.runs, TAG, bundles, self.root)
        self.assertIn("Pending release", self.stdout.getvalue())
        self.assertEqual(self.mutations, [])

    def test_existing_manifest_cannot_reference_missing_assets(self):
        bundles = self.bundles()
        self.existing_release(bundles)
        self.release["assets"] = [asset for asset in self.release["assets"]
                                  if asset["name"] != "chromix-win-x64.zip"]
        with self.assertRaisesRegex(ValueError, "references missing release assets"):
            release.publish(REPO, self.runs, TAG, bundles, self.root)
        self.assertEqual(self.mutations, [])

    def test_partial_draft_is_completed_only_after_all_assets_are_verified(self):
        bundles = self.bundles()
        self.existing_release({"chromix-linux-x64.zip": bundles["chromix-linux-x64.zip"]}, draft=True)
        release.publish(REPO, self.runs, TAG, bundles, self.root)
        uploads = [Path(args[3]).name for args in self.mutations if args[1] == "upload"]
        self.assertEqual(set(uploads), (release.ASSETS - {"chromix-linux-x64.zip"}) | {"SHA256SUMS"})
        self.assertEqual(self.mutations[-1][1], "edit")


if __name__ == "__main__":
    unittest.main()
