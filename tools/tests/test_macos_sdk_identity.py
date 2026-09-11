"""Complete SDK content hashing is independent of image and filesystem timestamps."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from tools import macos_sdk_identity as sdk


class MacOSSDKIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.root = self.work / "MacOSX.sdk"
        for name, content in {
                "SDKSettings.json": '{"Version": "26.0"}',
                "usr/include/header.h": "header one",
                "usr/lib/libSystem.tbd": "library one",
                "System/Library/Frameworks/Kit.framework/Versions/A/Headers/Kit.h": "framework header",
                "System/Library/Frameworks/Kit.framework/Versions/A/Kit.tbd": "framework library",
                "System/Library/Frameworks/Kit.framework/Versions/A/Modules/module.modulemap": "module Kit {}",
                "System/Library/PrivateFrameworks/Private.framework/Private.tbd": "private library",
                "custom/input.inc": "other input"}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        framework = self.root / "System/Library/Frameworks/Kit.framework"
        (framework / "Versions/Current").symlink_to("A", target_is_directory=True)
        (framework / "Headers").symlink_to("Versions/Current/Headers", target_is_directory=True)
        (framework / "Kit.tbd").symlink_to("Versions/Current/Kit.tbd")

    def identity(self):
        value = sdk.sdk_content_identity(self.root)
        self.assertTrue(sdk.validated_sdk_content(value), value)
        return value

    def test_full_tree_manifest_digest_is_reproducible(self):
        root = self.work / "small.sdk"
        (root / "include").mkdir(parents=True)
        header = root / "include/a.h"
        header.write_bytes(b"header")
        link = root / "alias"
        link.symlink_to("include", target_is_directory=True)
        records = [[".", "directory", root.stat().st_mode & 0o7777], ["alias", "symlink", "include", "include"],
                   ["include", "directory", (root / "include").stat().st_mode & 0o7777],
                   ["include/a.h", "file", header.stat().st_mode & 0o7777, 6, hashlib.sha256(b"header").hexdigest()]]
        digest = hashlib.sha256(b"".join(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
                                         + b"\n" for value in records)).hexdigest()
        value = sdk.sdk_content_identity(root)
        self.assertEqual(value, dict(schema_version=1, algorithm="sha256", scope="sdk-tree-v1", complete=True,
                                     sha256=digest, files=1, directories=2, symlinks=1, bytes=6))

    def test_copy_root_alias_and_mtime_changes_keep_content_identity(self):
        before = self.identity()
        for path in (self.root, *self.root.rglob("*")):
            os.utime(path, ns=(1, 2), follow_symlinks=False)
        self.assertEqual(self.identity(), before)
        other = self.work / "copy.sdk"
        shutil.copytree(self.root, other, symlinks=True)
        alias = self.work / "MacOSX26.sdk"
        alias.symlink_to(other, target_is_directory=True)
        self.assertEqual(sdk.sdk_content_identity(other), before)
        self.assertEqual(sdk.sdk_content_identity(alias), before)

    def test_all_regular_content_changes_are_detected_with_preserved_stats(self):
        before = self.identity()
        for path in self.root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            with self.subTest(path=path.relative_to(self.root)):
                content, info = path.read_bytes(), path.stat()
                path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                changed = self.identity()
                self.assertNotEqual(before["sha256"], changed["sha256"])
                self.assertEqual((changed["files"], changed["bytes"]), (before["files"], before["bytes"]))
                path.write_bytes(content)
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(before, self.identity())

    def test_entry_addition_removal_rename_and_mode_change_are_detected(self):
        before = self.identity()
        path = self.root / "usr/include/header.h"
        mode = path.stat().st_mode
        path.chmod(0o700)
        self.assertNotEqual(before["sha256"], self.identity()["sha256"])
        path.chmod(mode)
        renamed = path.with_name("renamed.h")
        path.rename(renamed)
        self.assertNotEqual(before["sha256"], self.identity()["sha256"])
        renamed.rename(path)
        extra = self.root / "usr/include/extra.h"
        extra.write_text("extra")
        self.assertNotEqual(before["sha256"], self.identity()["sha256"])
        extra.unlink()
        self.assertEqual(before, self.identity())
        empty = self.root / "empty"
        empty.mkdir()
        self.assertNotEqual(before["sha256"], self.identity()["sha256"])
        empty.rmdir()
        self.assertEqual(before, self.identity())

    def test_symlink_spelling_and_retarget_are_hashed_without_expanding_aliases(self):
        before = self.identity()
        link = self.root / "System/Library/Frameworks/Kit.framework/Headers"
        for target in ("./Versions/Current/Headers", "Versions/A/Headers", "Versions/A/Modules"):
            with self.subTest(target=target):
                link.unlink()
                link.symlink_to(target, target_is_directory=True)
                changed = self.identity()
                self.assertNotEqual(before["sha256"], changed["sha256"])
                self.assertEqual(changed["files"], before["files"])

    def test_internal_parent_alias_does_not_recurse(self):
        link = self.root / "usr/include/sdk"
        link.symlink_to("../..", target_is_directory=True)
        value = self.identity()
        self.assertEqual(value["files"], 8)
        self.assertEqual(value["symlinks"], 4)

    def test_external_broken_and_looping_links_are_not_complete_identities(self):
        external = self.work / "external"
        external.mkdir()
        (external / "header.h").write_text("external")
        link = self.root / "usr/include/linked"
        for target in (external, external / "header.h", "missing", "linked"):
            with self.subTest(target=target):
                link.symlink_to(target)
                value = sdk.sdk_content_identity(self.root)
                self.assertFalse(sdk.validated_sdk_content(value))
                self.assertFalse(value["complete"])
                self.assertNotIn("sha256", value)
                self.assertTrue(value["error"])
                link.unlink()

    def test_resolved_link_target_is_hashed_even_through_an_external_alias(self):
        target = self.work / "target"
        target.symlink_to(self.root / "usr/include/header.h")
        link = self.root / "alias"
        link.symlink_to(target)
        before = self.identity()
        target.unlink()
        target.symlink_to(self.root / "usr/lib/libSystem.tbd")
        changed = self.identity()
        self.assertNotEqual(before["sha256"], changed["sha256"])
        self.assertEqual(before["files"], changed["files"])
        self.assertEqual(os.readlink(link), str(target))

    def test_no_cross_inspection_stat_cache_and_hardlinks_read_once(self):
        path = self.root / "usr/include/header.h"
        os.link(path, path.with_name("hardlink.h"))
        with mock.patch.object(sdk.os, "open", wraps=os.open) as opened:
            before = self.identity()
        reads = [call.args[0] for call in opened.call_args_list]
        self.assertEqual(len(reads), before["files"] - 1)
        info = path.stat()
        path.write_text("header two")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with mock.patch.object(sdk.os, "open", wraps=os.open) as opened:
            changed = self.identity()
        self.assertNotEqual(before["sha256"], changed["sha256"])
        self.assertEqual(opened.call_count, len(reads))

    def test_missing_empty_non_directory_and_special_entries_are_incomplete(self):
        empty = self.work / "empty"
        empty.mkdir()
        for path in (self.work / "missing", empty, self.root / "SDKSettings.json"):
            with self.subTest(path=path):
                self.assertFalse(sdk.validated_sdk_content(sdk.sdk_content_identity(path)))
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "pipe")
            value = sdk.sdk_content_identity(self.root)
            self.assertFalse(value["complete"])
            self.assertIn("unsupported SDK entry", value["error"])

    def test_read_and_walk_errors_do_not_publish_partial_hashes(self):
        for operation in ("open", "scandir"):
            with self.subTest(operation=operation), \
                    mock.patch.object(sdk.os, operation, side_effect=PermissionError("denied")):
                value = sdk.sdk_content_identity(self.root)
            self.assertFalse(value["complete"])
            self.assertEqual(value["error"], "denied")
            self.assertNotIn("sha256", value)

    def test_mutation_during_read_does_not_publish_partial_hash(self):
        original = os.fstat
        calls = 0
        def changed(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                with (self.root / "SDKSettings.json").open("ab") as stream:
                    stream.write(b"changed")
            return original(fd)
        with mock.patch.object(sdk.os, "fstat", side_effect=changed):
            value = sdk.sdk_content_identity(self.root)
        self.assertFalse(value["complete"])
        self.assertIn("changed during hashing", value["error"])
        self.assertNotIn("sha256", value)

    def test_edit_after_file_was_hashed_is_detected_by_final_stat_pass(self):
        original = os.scandir
        changed = False
        settings = self.root / "SDKSettings.json"
        def scandir(path):
            nonlocal changed
            if Path(path) != self.root and not changed:
                changed = True
                settings.write_text("changed")
            return original(path)
        with mock.patch.object(sdk.os, "scandir", side_effect=scandir):
            value = sdk.sdk_content_identity(self.root)
        self.assertFalse(value["complete"])
        self.assertIn("changed during hashing", value["error"])

    def test_fingerprint_validation_is_typed_versioned_and_complete(self):
        identity = self.identity()
        for key in identity:
            value = copy.deepcopy(identity)
            value.pop(key)
            with self.subTest(missing=key):
                self.assertFalse(sdk.validated_sdk_content(value))
        changes = {"schema_version": (True, 2, "1"), "algorithm": ("md5",), "scope": ("metadata",),
                   "complete": (False, 1), "sha256": ("f" * 63, "G" * 64, "a" * 64 + "\n", None),
                   "files": (True, 0, -1), "directories": (0, "1"), "symlinks": (-1, False), "bytes": (-1, 1.0)}
        for key, values in changes.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertFalse(sdk.validated_sdk_content(dict(identity, **{key: value})))
        for value in (None, [], {}, "f" * 64):
            self.assertFalse(sdk.validated_sdk_content(value))


if __name__ == "__main__":
    unittest.main()
