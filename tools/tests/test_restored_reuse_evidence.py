import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import restored_reuse_evidence as evidence
from tools import restore_upstream_cache as restore


NINJAS = [Path(f"/tmp/chromix-ninja-v{version}/ninja") for version in ("1.11.1", "1.12.1", "1.13.2")]
AVAILABLE = [path for path in NINJAS if path.is_file()]


class RestoredReuseEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.out = self.work / "src/out/Default"
        self.out.mkdir(parents=True)
        self.ninja = self.root / "ninja"
        self.ninja.write_bytes(b"fixture, never executed")
        self.receipt = {"identity": {"head_sha": "a" * 40, "platform": "linux", "arch": "x64"},
                        "manifest": {"sha256": "b" * 64, "path": "/original/manifest.json"}}
        self.verify = mock.patch.object(restore, "verify_restored", return_value=self.receipt).start()
        self.addCleanup(mock.patch.stopall)
        self.inputs = ["obj/a.o", "obj/b.obj", "../../source.cc"]
        self.version = 5
        self.make_log()
        self.query = mock.patch.object(evidence, "query", side_effect=self.query_result).start()

    def query_result(self, ninja, out, args, limit):
        if args == ["--version"]:
            return b"1.11.1\n"
        self.assertEqual(args[:2], ["-t", "inputs"])
        self.assertEqual(ninja, self.ninja)
        self.assertEqual(out, self.out)
        return ("\n".join(self.inputs) + "\n").encode() if self.inputs else b""

    def make_log(self, names=("obj/a.o", "obj/b.obj"), *, extra=()):
        self.records = []
        for index, name in enumerate((*names, *extra)):
            path = self.out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"tiny-object")
            mtime = 1_700_000_000_000_000_000 + index
            os.utime(path, ns=(mtime, mtime))
            self.records.append(f"{10 + index}\t{20 + index}\t{mtime}\t{name}\tdeadbeef\n")
        self.log = self.out / ".ninja_log"
        self.log.write_text(f"# ninja log v{self.version}\n" + "".join(self.records))

    def before(self, **kwargs):
        return evidence.before(self.work, "linux", "x64", self.ninja, **kwargs)

    def after(self, code=0, **kwargs):
        return evidence.after(self.work, "linux", "x64", self.ninja, exit_code=code, **kwargs)

    def result(self):
        return json.loads((self.work / "upstream-reuse/result.json").read_text())

    def cli(self, phase, code=None):
        args = [phase, "--workdir", str(self.work), "--platform", "linux", "--arch", "x64",
                "--ninja", str(self.ninja), "--target", "chrome"]
        if code is not None:
            args += ["--exit-code", str(code)]
        with mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()):
            return evidence.main(args)

    def test_retention_full_latest_records_and_scope_v5_v6_v7(self):
        for version in (5, 6, 7):
            with self.subTest(version=version):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.version = version
                self.make_log()
                with self.log.open("a") as stream:
                    stream.write(self.records[0].replace("10\t20\t", "30\t40\t"))
                before_log = self.log.read_bytes()
                baseline = self.before()
                self.assertFalse(baseline["retention_proven"])
                sample = baseline["samples"][0]
                self.assertEqual(sample["record"], {"start": 30, "end": 40,
                    "mtime": 1_700_000_000_000_000_000, "output": "obj/a.o", "hash": "deadbeef", "version": version})
                self.assertEqual(sample["file"]["sha256"], hashlib.sha256(b"tiny-object").hexdigest())
                self.assertEqual(self.log.read_bytes(), before_log)
                report = self.after()
                self.assertEqual(report["retained_count"], 2)
                self.assertTrue(report["retention_proven"])
                self.assertTrue(report["invocation_successful"])
                self.assertTrue(report["upstream_source_verified"])
                self.assertFalse(report["original_donor_records_verified"])
                self.assertEqual(report["scope"], evidence.SCOPE)
                self.assertLess(len(json.dumps(report)), 16 * 1024)
                self.verify.assert_called_with(self.work, "linux", "x64", repo=evidence.REPO)

    def test_rebuild_only_start_end_changed_does_not_count(self):
        self.before()
        self.log.write_text(self.log.read_text() + "".join(
            line.replace("\t20\t", "\t80\t").replace("\t21\t", "\t81\t") for line in self.records))
        report = self.after()
        self.assertFalse(report["retention_proven"])
        self.assertEqual(report["status"], "unproven")
        self.assertEqual({item["status"] for item in report["samples"]}, {"appended_record_changed"})

    def test_content_change_even_with_same_size_and_mtime(self):
        self.before()
        path = self.out / "obj/a.o"
        info = path.stat()
        path.write_bytes(b"evil-object")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        report = self.after()
        self.assertEqual(report["retained_count"], 1)
        self.assertEqual(report["samples"][0]["status"], "content_changed")

    def test_size_or_mtime_change_excludes_object_without_hashing(self):
        for kind in ("size", "mtime"):
            with self.subTest(kind=kind):
                self.before()
                path = self.out / "obj/a.o"
                if kind == "size":
                    path.write_bytes(b"different size")
                else:
                    info = path.stat()
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
                with mock.patch.object(evidence, "file_record", wraps=evidence.file_record) as hashing:
                    report = self.after()
                self.assertEqual(report["samples"][0]["status"], "file_metadata_changed")
                self.assertEqual(hashing.call_count, 1)

    def test_out_of_graph_and_missing_files_or_records_never_sampled(self):
        self.make_log(extra=("obj/ignored.o",))
        (self.out / "obj/b.obj").unlink()
        (self.out / "obj/no-log.o").write_bytes(b"not logged")
        self.inputs.append("obj/no-log.o")
        baseline = self.before()
        self.assertEqual([item["output"] for item in baseline["samples"]], ["obj/a.o"])
        self.assertEqual(baseline["skipped"]["missing_record"], 1)
        self.assertEqual(baseline["skipped"]["missing_file"], 1)
        self.assertEqual(self.after()["retained_count"], 1)

    def test_objects_removed_from_graph_after_or_between_invocations(self):
        self.before()
        self.inputs = ["obj/b.obj"]
        report = self.after()
        self.assertEqual(report["retained_count"], 0)
        self.assertEqual(report["samples"][0]["status"], "not_in_target_inputs")
        self.before()
        report = self.after()
        self.assertEqual(report["retained_count"], 0)
        self.assertEqual(report["samples"][0]["status"], "not_in_target_inputs")
        self.assertEqual(report["disqualification_reasons"], {"not_in_target_inputs": 1, "target_inputs_changed": 1})

    def test_missing_after_files_or_records_are_not_retained(self):
        self.before()
        (self.out / "obj/a.o").unlink()
        # Replacing an original record breaks continuity even without a size change.
        self.log.write_text(self.log.read_text().replace("obj/b.obj", "obj/z.obj"))
        report = self.after()
        self.assertEqual([item["status"] for item in report["samples"]], ["log_prefix_mismatch"] * 2)
        self.assertFalse(report["retention_proven"])

    def test_log_reset_compaction_or_version_change_is_unproven(self):
        for kind in ("empty", "compacted", "version"):
            with self.subTest(kind=kind):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.make_log()
                self.log.write_text(self.log.read_text() + self.records[0])
                self.before()
                if kind == "empty":
                    self.log.write_text("# ninja log v5\n")
                elif kind == "compacted":
                    self.log.write_text("# ninja log v5\n" + "".join(self.records))
                else:
                    self.log.write_text(self.log.read_text().replace("log v5", "log v6"))
                report = self.after()
                self.assertFalse(report["retention_proven"])
                self.assertEqual({item["status"] for item in report["samples"]}, {"log_prefix_mismatch"})

    def test_resume_never_resets_baseline_or_counts_rebuilt_outputs(self):
        baseline = self.before()
        path = self.work / "upstream-reuse/baseline.json"
        raw, info = path.read_bytes(), path.stat()
        self.after(124)
        self.log.write_text(self.log.read_text() + self.records[0].replace("10\t20\t", "30\t40\t"))
        self.assertEqual(self.before(), baseline)
        report = self.after()
        self.assertEqual(report["retained_count"], 1)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mtime_ns, info.st_mtime_ns)
        self.assertEqual(self.before(), baseline)
        self.assertEqual(self.result()["status"], "unproven")
        self.assertFalse(self.result()["retention_proven"])

    def test_resumed_workspace_relocation_and_new_run_are_supported(self):
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "101"}, clear=True):
            baseline = self.before()
            self.after(124)
        destination = self.root / "relocated"
        self.work.rename(destination)
        self.work = destination
        self.out = self.work / "src/out/Default"
        self.receipt["manifest"]["path"] = "/another/manifest.json"
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "102"}, clear=True):
            self.assertEqual(self.before(), baseline)
            report = self.after()
        self.assertEqual(report["baseline_run"]["GITHUB_RUN_ID"], "101")
        self.assertEqual(report["run"]["GITHUB_RUN_ID"], "102")
        self.assertTrue(report["retention_proven"])

    def test_nonzero_exit_is_incomplete_even_when_objects_unchanged(self):
        for code in (1, 124, -9):
            with self.subTest(code=code):
                self.before()
                report = self.after(code)
                self.assertEqual(report["status"], "incomplete")
                self.assertFalse(report["retention_proven"])
                self.assertFalse(report["invocation_successful"])
                self.assertEqual(report["retained_count"], 2)
                self.assertEqual(report["exit_code"], code)

    def test_zero_samples_is_unproven(self):
        self.inputs = []
        self.assertEqual(self.before()["samples"], [])
        self.assertEqual(self.after()["status"], "unproven")

    def test_receipt_identity_change_fails_closed_and_preserves_baseline(self):
        self.before()
        path = self.work / "upstream-reuse/baseline.json"
        raw = path.read_bytes()
        self.receipt["identity"]["head_sha"] = "c" * 40
        self.assertEqual(self.cli("before"), 1)
        self.assertEqual(self.result()["status"], "error")
        self.assertFalse(self.result()["retention_proven"])
        self.assertEqual(path.read_bytes(), raw)
        self.verify.side_effect = restore.Miss("invalid receipt")
        self.assertEqual(self.cli("after", 0), 1)

    def test_local_verification_errors_return_nonzero_without_traceback(self):
        self.verify.side_effect = restore.LocalError("unsupported target")
        self.assertEqual(self.cli("before"), 1)
        self.assertFalse(self.result()["retention_proven"])
        self.assertEqual(self.result()["status"], "error")

    def test_target_mismatch_or_unpaired_after_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            self.after()
        self.before()
        with self.assertRaisesRegex(evidence.EvidenceError, "identity, targets"):
            self.after(targets=("other",))
        self.after()
        self.assertEqual(self.cli("after", 0), 1)
        self.assertFalse(self.result()["retention_proven"])

    def test_repeated_targets_are_normalized_for_windows(self):
        first = evidence.before(self.work, "windows", "x64", self.ninja, targets=("chrome", "other", "chrome"))
        report = evidence.after(self.work, "windows", "x64", self.ninja, targets=("other", "chrome"), exit_code=0)
        self.assertEqual(first["targets"], ["chrome", "other"])
        self.assertTrue(report["retention_proven"])

    def test_unsafe_object_input_and_log_paths_rejected(self):
        for name in ("../escape.o", "/escape.o", "obj/../escape.o", "obj//a.o", "./obj/a.o",
                     "C:/escape.obj", "obj\\escape.obj", "'obj/space name.o'", "obj/$a.o"):
            with self.subTest(name=name):
                self.inputs = [name]
                with self.assertRaises(ValueError):
                    self.before()
                self.inputs = ["obj/a.o"]
                self.log.write_text("# ninja log v5\n1\t2\t3\t" + name + "\tabc\n")
                with self.assertRaises(ValueError):
                    self.before()
                self.make_log()

    def test_symlinked_or_hardlinked_objects_and_linked_parents_rejected(self):
        original = self.out / "obj/a.o"
        target = self.root / "external"
        target.write_bytes(b"tiny-object")
        for kind in ("symlink", "hardlink", "parent"):
            with self.subTest(kind=kind):
                original.unlink()
                if kind == "symlink":
                    original.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, original)
                else:
                    (self.out / "obj").rename(self.out / "oldobj")
                    (self.out / "obj").symlink_to(self.out / "oldobj", target_is_directory=True)
                with self.assertRaises(ValueError):
                    self.before()
                if kind == "parent":
                    (self.out / "obj").unlink()
                    (self.out / "oldobj").rename(self.out / "obj")
                else:
                    original.unlink()
                original.write_bytes(b"tiny-object")

    def test_linked_report_or_metadata_never_overwrites_external_file(self):
        target = self.root / "external"
        target.write_text("keep")
        directory = self.work / "upstream-reuse"
        directory.mkdir()
        (directory / "result.json").symlink_to(target)
        self.assertEqual(self.cli("before"), 1)
        self.assertEqual(target.read_text(), "keep")
        (directory / "result.json").unlink()
        self.log.unlink()
        self.log.symlink_to(target)
        self.assertEqual(self.cli("before"), 1)
        self.assertEqual(target.read_text(), "keep")

    def test_invalid_logs_hard_fail_and_replace_old_success_report(self):
        invalid = ["# ninja log v8\n", "# ninja log v5\n1\t2\t3\ta.o\tx\n",
                   "# ninja log v5\n2\t1\t3\ta.o\tabc\n", "# ninja log v5\n1\t2\t-1\ta.o\tabc\n",
                   "# ninja log v5\n1\t2\t3\ta.o\tabc", "# ninja log v5\n1\t2\t3\n"]
        for text in invalid:
            with self.subTest(text=text):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.make_log()
                self.before()
                self.after()
                self.log.write_text(text)
                self.assertEqual(self.cli("before"), 1)
                self.assertFalse(self.result()["retention_proven"])
                self.assertEqual(self.result()["status"], "error")

    def test_corrupt_baseline_is_never_replaced(self):
        self.before()
        path = self.work / "upstream-reuse/baseline.json"
        baseline = json.loads(path.read_text())
        for content in ("{", '{"owner":1,"owner":2}', json.dumps(dict(baseline, samples=[{}])),
                        json.dumps(dict(baseline, samples=baseline["samples"] * 2))):
            with self.subTest(content=content[:40]):
                path.write_text(content)
                self.assertEqual(self.cli("before"), 1)
                self.assertEqual(path.read_text(), content)
                self.assertFalse(self.result()["retention_proven"])

    def test_byte_and_sample_caps(self):
        self.make_log(names=("obj/a.o", "obj/b.obj", "obj/c.o"))
        self.inputs = ["obj/a.o", "obj/b.obj", "obj/c.o"]
        with mock.patch.object(evidence, "MAX_HASH_BYTES", 15), \
                mock.patch.object(evidence, "MAX_FILE_BYTES", 12):
            baseline = self.before()
            self.assertEqual(baseline["object_hash_bytes"], 11)
            self.assertEqual(baseline["skipped"]["total_byte_cap"], 2)
            self.assertLessEqual(self.after()["object_hash_bytes"], 15)
        shutil.rmtree(self.work / "upstream-reuse")
        (self.out / "obj/a.o").write_bytes(b"too large for cap")
        with mock.patch.object(evidence, "MAX_SAMPLES", 1), mock.patch.object(evidence, "MAX_FILE_BYTES", 12):
            baseline = self.before()
        self.assertEqual([sample["output"] for sample in baseline["samples"]], ["obj/b.obj"])
        self.assertEqual(baseline["skipped"]["file_byte_cap"], 1)

    def test_log_and_json_byte_caps_hard_fail(self):
        with mock.patch.object(evidence, "MAX_LOG_BYTES", 16):
            self.assertEqual(self.cli("before"), 1)
        shutil.rmtree(self.work / "upstream-reuse")
        self.before()
        with mock.patch.object(evidence, "MAX_JSON_BYTES", 16):
            self.assertEqual(self.cli("before"), 1)

    def test_missing_baseline_on_resume_is_not_recaptured(self):
        self.before()
        self.after()
        path = self.work / "upstream-reuse/baseline.json"
        path.unlink()
        self.assertEqual(self.cli("before"), 1)
        self.assertFalse(path.exists())
        self.assertFalse(self.result()["retention_proven"])

    def test_malformed_baseline_and_pending_metadata_fail_closed(self):
        self.before()
        baseline_path = self.work / "upstream-reuse/baseline.json"
        pending_path = self.work / "upstream-reuse/result.json"
        baseline, pending = baseline_path.read_bytes(), pending_path.read_bytes()
        for path, key, value in ((baseline_path, "log", []), (baseline_path, "membership", None),
                                 (pending_path, "log", "bad"), (pending_path, "membership", 3),
                                 (pending_path, "eligible_outputs", [True])):
            with self.subTest(key=key, value=value):
                baseline_path.write_bytes(baseline)
                pending_path.write_bytes(pending)
                data = json.loads(path.read_text())
                data[key] = value
                path.write_text(json.dumps(data))
                self.assertEqual(self.cli("after", 0), 1)
                self.assertFalse(self.result()["retention_proven"])

    def test_crlf_logs_are_supported_without_losing_full_records(self):
        self.log.write_bytes(self.log.read_bytes().replace(b"\n", b"\r\n"))
        baseline = self.before()
        self.assertEqual(baseline["samples"][0]["record"]["start"], 10)
        self.assertTrue(self.after()["retention_proven"])

    def test_only_safe_run_and_head_identifiers_are_recorded(self):
        values = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
                  "GITHUB_JOB": "compile-x64", "GITHUB_HEAD_REF": "bad\nhead", "GH_TOKEN": "secret",
                  "GITHUB_TOKEN": "secret", "PASSWORD": "secret", "CREDENTIALS": "secret"}
        with mock.patch.dict(os.environ, values, clear=True):
            report = self.before()
        self.assertEqual(set(report["run"]), {"GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "GITHUB_JOB"})
        self.assertNotIn("secret", json.dumps(report))

    def test_disqualification_survives_restored_bytes_mtime_and_record_across_calls(self):
        for phase in ("before", "after"):
            for kind in ("content", "mtime", "record", "missing"):
                with self.subTest(phase=phase, kind=kind):
                    shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                    self.make_log()
                    baseline = self.before()
                    path = self.out / "obj/a.o"
                    raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
                    if kind == "content":
                        path.write_bytes(b"evil-object")
                        os.utime(path, ns=(mtime, mtime))
                    elif kind == "mtime":
                        os.utime(path, ns=(mtime + 1, mtime + 1))
                    elif kind == "record":
                        with self.log.open("a") as stream:
                            stream.write(self.records[0].replace("10\t20\t", "30\t40\t"))
                    else:
                        path.unlink()
                    self.before() if phase == "before" else self.after()
                    rejected = self.result()["disqualified"]
                    self.assertIn("obj/a.o", rejected)
                    path.write_bytes(raw)
                    os.utime(path, ns=(mtime, mtime))
                    with self.log.open("a") as stream:
                        stream.write(self.records[0])
                    self.assertEqual(self.before(), baseline)
                    report = self.after()
                    self.assertEqual(report["retained_count"], 1)
                    self.assertEqual(report["disqualified"], rejected)
                    self.before()
                    self.assertEqual(self.after()["disqualified"], rejected)

    def test_changed_then_baseline_appended_in_one_interval_is_disqualified(self):
        for phase in ("before", "after"):
            with self.subTest(phase=phase):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.make_log()
                baseline = self.before()
                with self.log.open("a") as stream:
                    stream.write(self.records[0].replace("10\t20\t", "30\t40\t") + self.records[0])
                self.before() if phase == "before" else self.after()
                report = self.result()
                self.assertEqual(report["samples"][0]["record"], baseline["samples"][0]["record"])
                self.assertEqual(report["disqualified"], {"obj/a.o": "appended_record_changed"})
                self.before()
                self.assertEqual(self.after()["retained_count"], 1)

    def test_truncation_then_growth_breaks_full_previous_prefix_permanently(self):
        baseline = self.before()
        with self.log.open("a") as stream:
            stream.write("1\t2\t0\tunrelated\tabc\n")
        previous = self.after()
        # The original baseline is intact, but the last observation's suffix changed.
        self.log.write_text(self.log.read_text().replace("unrelated", "different") + self.records[0] * 4)
        self.before()
        report = self.result()
        self.assertGreater(report["log"]["size_bytes"], previous["log"]["size_bytes"])
        self.assertEqual(report["log"]["prefix_size_bytes"], previous["log"]["size_bytes"])
        self.assertFalse(report["log"]["prefix_matches_previous"])
        self.assertEqual(report["disqualification_reasons"], {"log_prefix_mismatch": 2})
        self.log.write_text("# ninja log v5\n" + "".join(self.records))
        self.before()
        self.assertFalse(self.after()["retention_proven"])
        self.assertEqual(json.loads((self.work / "upstream-reuse/baseline.json").read_text()), baseline)

    def test_timeout_or_killed_build_resume_inspects_appended_records(self):
        for completed_after in (False, True):
            with self.subTest(completed_after=completed_after):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.make_log()
                self.before()
                if completed_after:
                    self.assertEqual(self.after(124)["status"], "incomplete")
                with self.log.open("a") as stream:
                    stream.write(self.records[0].replace("10\t20\t", "30\t40\t") + self.records[0])
                self.before()
                self.assertEqual(self.result()["disqualified"], {"obj/a.o": "appended_record_changed"})
                self.assertEqual(self.after()["retained_count"], 1)

    def test_original_history_before_baseline_does_not_disqualify(self):
        for version in (5, 6, 7):
            with self.subTest(version=version):
                shutil.rmtree(self.work / "upstream-reuse", ignore_errors=True)
                self.version = version
                self.make_log()
                self.log.write_text(f"# ninja log v{version}\n" + self.records[0].replace(
                    "10\t20\t", "30\t40\t") + "".join(self.records))
                baseline = self.before()
                self.assertEqual(baseline["log"]["sha256"], hashlib.sha256(self.log.read_bytes()).hexdigest())
                for _ in range(2):
                    report = self.after()
                    self.assertTrue(report["retention_proven"])
                    self.assertEqual(report["disqualified"], {})
                    self.assertEqual(report["log"]["prefix_sha256"], baseline["log"]["sha256"])
                    self.before()

    def test_zero_log_mtimes_are_valid_but_not_object_candidates(self):
        zero = self.records[0].replace("1700000000000000000", "0")
        self.log.write_text("# ninja log v5\n" + zero + self.records[1] + "1\t2\t0\tunrelated\tabc\n")
        baseline = self.before()
        self.assertEqual([sample["output"] for sample in baseline["samples"]], ["obj/b.obj"])
        self.assertEqual(baseline["skipped"]["nonpositive_log_mtime"], 1)
        with self.log.open("a") as stream:
            stream.write("1\t2\t0\tanother\tabc\n")
        self.assertEqual(self.after()["retained_count"], 1)

    def test_nonpositive_appended_object_mtime_permanently_disqualifies(self):
        self.before()
        with self.log.open("a") as stream:
            stream.write(self.records[0].replace("1700000000000000000", "0") + self.records[0])
        report = self.after()
        self.assertEqual(report["disqualified"], {"obj/a.o": "appended_record_changed"})
        self.assertEqual(report["retained_count"], 1)

    def test_invalid_disqualification_reason_or_counter_fails_closed(self):
        self.before()
        path = self.work / "upstream-reuse/result.json"
        pending = path.read_bytes()
        for change in ({"disqualified": {"obj/a.o": "retained"}, "disqualified_count": 1},
                       {"disqualified_count": 1}, {"disqualification_reasons": {"log_prefix_mismatch": 2}}):
            with self.subTest(change=change):
                data = json.loads(pending)
                data.update(change)
                path.write_text(json.dumps(data))
                self.assertEqual(self.cli("after", 0), 1)
                self.assertFalse(self.result()["retention_proven"])

    def test_missing_or_error_previous_state_cannot_restart_observations(self):
        self.before()
        path = self.work / "upstream-reuse/result.json"
        raw = path.read_bytes()
        path.unlink()
        self.assertEqual(self.cli("before"), 1)
        self.assertFalse(self.result()["retention_proven"])
        self.assertEqual(self.cli("before"), 1)
        data = json.loads(raw)
        data["disqualified"] = []
        path.write_text(json.dumps(data))
        self.assertEqual(self.cli("before"), 1)

    def test_ninja_110_requires_newer_selector_before_inputs_query(self):
        self.query.side_effect = lambda *args: b"1.10.2\n"
        self.assertEqual(self.cli("before"), 1)
        self.assertEqual(self.query.call_count, 1)
        self.assertIn("requires 1.11", self.result()["reason"])
        self.assertFalse(self.result()["retention_proven"])

    def test_selected_chromium_ninja_version_suffix_is_supported(self):
        original = self.query.side_effect
        for minor in (11, 12, 13):
            with self.subTest(minor=minor):
                self.query.side_effect = lambda ninja, out, args, limit: (
                    f"1.{minor}.1.chromium.4\n".encode() if args == ["--version"] else original(ninja, out, args, limit))
                self.before()
                self.assertEqual(self.result()["ninja"]["version"], f"1.{minor}.1.chromium.4")
                self.assertTrue(self.after()["retention_proven"])

    def test_cli_supports_both_phase_spellings_and_reports_build_failure_without_collector_failure(self):
        self.assertEqual(self.cli("before"), 0)
        self.assertEqual(self.cli("after", 124), 0)
        self.assertEqual(self.result()["status"], "incomplete")
        args = ["--phase", "before", "--workdir", str(self.work), "--platform", "linux", "--arch", "x64",
                "--ninja", str(self.ninja), "--target", "chrome"]
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(evidence.main(args), 0)
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            evidence.main([*args, "--exit-code", "0"])
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            evidence.main([value if value != "before" else "after" for value in args])


@unittest.skipUnless(AVAILABLE and os.name == "posix", "official tiny-fixture Ninja binaries unavailable")
class RealNinjaEvidenceTest(unittest.TestCase):
    def make_work(self, root: Path, ninja: Path):
        work = root / "work"
        out = work / "src/out/Default"
        out.mkdir(parents=True)
        (out / "source").write_text("tiny object, no compiler\n")
        (out / "build.ninja").write_text(
            "rule copy\n  command = cp $in $out\n"
            "build a.o: copy source\n"
            "build b.obj: copy source\n"
            "build ordered.o: copy source\n"
            "build ignored.o: copy source\n"
            "build archive: copy a.o | b.obj || ordered.o\n"
            "build chrome: phony archive\n"
            "build other: copy ignored.o\n")
        subprocess.run([str(ninja), "chrome", "other"], cwd=out, check=True, capture_output=True)
        identity, _, manifest = restore.identities(restore.REPO, "linux", "x64")
        src = work / "src"
        (src / "chrome").mkdir()
        (src / "chrome/VERSION").write_text("\n".join(f"{name}={value}" for name, value in zip(
            ("MAJOR", "MINOR", "BUILD", "PATCH"), identity["chromium_version"].split("."))) + "\n")
        (src / "BUILD.gn").write_text('group("chrome") {}\n')
        args = 'target_cpu = "x64"\n'
        (out / "args.gn").write_text(args)
        (out / ".ninja_deps").write_bytes(b"# ninjadeps\n\x04\0\0\0")
        receipt = {"schema_version": 1, "owner": restore.OWNER, "status": "restored",
                   "extraction_scope": restore.fetcher.SOURCE_SCOPE, "identity": identity, "manifest": manifest,
                   "platform": "linux", "arch": "x64", "external_symlink_paths": [],
                   "original_args": {"path": "out/Default/args.gn", "text": args, "bytes": len(args),
                                     "sha256": hashlib.sha256(args.encode()).hexdigest(),
                                     "assignments": restore.parse_gn_assignments(args)}}
        (src / restore.MARKER).write_text(json.dumps(receipt))
        return work, out

    def test_real_target_closure_noop_retention_and_rebuild_v5_v6_v7(self):
        for ninja in AVAILABLE:
            with self.subTest(ninja=ninja), tempfile.TemporaryDirectory() as tmp:
                work, out = self.make_work(Path(tmp), ninja)
                names, metadata = evidence.target_inputs(ninja, out, ["chrome"])
                self.assertEqual(names, {"a.o", "b.obj", "ordered.o"})
                self.assertFalse(metadata["validation_inputs_included"])
                union, _ = evidence.target_inputs(ninja, out, ["chrome", "other"])
                self.assertIn("ignored.o", union)
                baseline = evidence.before(work, "linux", "x64", ninja)
                expected = {"chromix-ninja-v1.11.1": 5, "chromix-ninja-v1.12.1": 6, "chromix-ninja-v1.13.2": 7}
                self.assertEqual(baseline["log"]["version"], expected[ninja.parent.name])
                log = (out / ".ninja_log").read_bytes()
                result = subprocess.run([str(ninja), "chrome"], cwd=out, check=True, capture_output=True)
                self.assertIn(b"no work to do", result.stdout)
                report = evidence.after(work, "linux", "x64", ninja, exit_code=result.returncode)
                self.assertEqual(report["retained_count"], 3)
                self.assertTrue(report["retention_proven"])
                self.assertEqual((out / ".ninja_log").read_bytes(), log)
                evidence.before(work, "linux", "x64", ninja)
                (out / "a.o").unlink()
                result = subprocess.run([str(ninja), "chrome"], cwd=out, check=True, capture_output=True)
                report = evidence.after(work, "linux", "x64", ninja, exit_code=result.returncode)
                self.assertEqual(report["retained_count"], 2)
                self.assertEqual(report["samples"][0]["status"], "appended_record_changed")
                baseline_path = work / "upstream-reuse/baseline.json"
                self.assertEqual(json.loads(baseline_path.read_text()), baseline)

    def test_query_output_cap_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, out = self.make_work(Path(tmp), AVAILABLE[0])
            with self.assertRaisesRegex(evidence.EvidenceError, "byte cap"):
                evidence.query(AVAILABLE[0], out, ["-t", "inputs", "chrome"], 4)
            sleeper = Path(tmp) / "sleeping-ninja"
            sleeper.write_text("#!/bin/sh\nexec sleep 10\n")
            sleeper.chmod(0o755)
            with mock.patch.object(evidence, "NINJA_TIMEOUT", 0.01):
                with self.assertRaisesRegex(evidence.EvidenceError, "failed or timed out"):
                    evidence.query(sleeper, out, ["--version"], 4096)


if __name__ == "__main__":
    unittest.main()
