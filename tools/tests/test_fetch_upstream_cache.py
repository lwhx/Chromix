import copy
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import fetch_upstream_cache as cache

VERSION = b"MAJOR=152\nMINOR=0\nBUILD=7977\nPATCH=82\n"
NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
MTIME = 1700000000123456700


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def metadata(pin):
    repo = {"full_name": pin["repository"], "id": pin["repository_id"], "private": False}
    run = {key: pin[key] for key in ("head_sha", "head_branch", "event")}
    run.update(id=pin["run_id"], path=pin["workflow_path"], status="completed", conclusion="success",
               repository=repo.copy(), head_repository=repo.copy())
    artifact = {**pin["artifact"], "expired": False, "expires_at": "2099-01-01T00:00:00Z",
                "workflow_run": {"id": pin["run_id"], "head_sha": pin["head_sha"],
                                 "head_branch": pin["head_branch"],
                                 "repository_id": pin["repository_id"],
                                 "head_repository_id": pin["repository_id"]}}
    return run, artifact


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if kind in ("file", "dir") else 0o777
            info.pax_headers = {"mtime": "1700000000.123456700"}
            info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE,
                         "hard": tarfile.LNKTYPE, "file": tarfile.REGTYPE,
                         "fifo": tarfile.FIFOTYPE}[kind]
            if kind in ("sym", "hard"):
                info.linkname = data
            if kind == "file":
                info.size = len(data)
            archive.addfile(info, io.BytesIO(data) if kind == "file" else None)
    return output.getvalue()


def zip_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, kind, data in entries:
            info = zipfile.ZipInfo(name + ("/" if kind == "dir" else ""))
            info.create_system = 3
            info.external_attr = {"file": stat.S_IFREG | 0o755, "dir": stat.S_IFDIR | 0o755,
                                  "sym": stat.S_IFLNK | 0o777}[kind] << 16
            ntfs = struct.pack("<IHHQQQ", 0, 1, 24, MTIME // 100 + 116444736000000000, 0, 0)
            info.extra = struct.pack("<HH", 10, len(ntfs)) + ntfs
            archive.writestr(info, data)
    return output.getvalue()


class Response(io.BytesIO):
    status = 200


class FetchUpstreamCacheTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "build").mkdir()
        for name in ("CHROMIUM_VERSION", "build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
            shutil.copyfile(cache.ROOT / name, self.root / name)
        self.manifest = json.loads((self.root / "build/upstream-cache.json").read_text())
        self.destination = self.root / "cache"
        self.pin, self.identity = cache.load_manifest("windows", "x64", root=self.root)
        disk = mock.patch.object(cache.shutil, "disk_usage", return_value=mock.Mock(free=1024**4))
        self.disk_usage = disk.start()
        self.addCleanup(disk.stop)

    def save_manifest(self):
        (self.root / "build/upstream-cache.json").write_text(json.dumps(self.manifest))

    def fixture_client(self, platform="windows", arch="x64", source="src", extra=()):
        entries = [(source, "dir", b""), (source + "/BUILD.gn", "file", b"build"),
                   (source + "/chrome/VERSION", "file", VERSION),
                   (source + "/chrome/browser/file.cc", "file", b"source"),
                   (source + "/out/Default/args.gn", "file", b"target_cpu=\"x64\""),
                   (source + "/out/Default/build.ninja", "file", b"build-ninja"),
                   (source + "/out/Default/.ninja_log", "file", b"ninja-log"),
                   (source + "/out/Default/.ninja_deps", "file", b"ninja-deps"),
                   (source + "/out/Default/obj/file.o", "file", b"object"),
                   (source + "/out/Default/gen/generated.h", "file", b"generated"),
                   ("build/download_cache/package.tar.xz", "file", b"excluded"), *extra]
        if platform == "windows":
            inner = zip_bytes(entries)
        else:
            inner = subprocess.check_output([shutil.which("zstd"), "-q", "-c"], input=tar_bytes(entries))
        artifact = self.manifest["sources"][platform]["artifacts"][arch]
        outer = zip_bytes([(artifact["inner_archive"], "file", inner)])
        artifact["digest"], artifact["size_in_bytes"] = digest(outer), len(outer)
        self.save_manifest()
        pin, _ = cache.load_manifest(platform, arch, root=self.root)
        run, artifact = metadata(pin)
        client = cache.GitHub("fixture-token")
        client.json = mock.Mock(side_effect=[run, artifact])
        client.open = mock.Mock(side_effect=lambda *a, **kw: Response(outer))
        return client

    def test_all_five_pins_match_and_windows_arm64_misses(self):
        for platform, arch in (("linux", "x64"), ("linux", "arm64"), ("macos", "x64"),
                               ("macos", "arm64"), ("windows", "x64")):
            pin, identity = cache.load_manifest(platform, arch, root=self.root)
            self.assertEqual(identity["artifact_digest"], pin["artifact"]["digest"])
        with self.assertRaisesRegex(cache.CacheMiss, "unsupported_target"):
            cache.load_manifest("windows", "arm64", root=self.root)

    def test_pin_mismatches_fail_before_network(self):
        for target in ("linux", "macos", "windows"):
            with self.subTest(target=target):
                saved = self.manifest["sources"][target]["head_sha"]
                self.manifest["sources"][target]["head_sha"] = "0" * 40
                self.save_manifest()
                with self.assertRaisesRegex(cache.CacheMiss, "pin_mismatch"):
                    cache.load_manifest("windows", "x64", root=self.root)
                self.manifest["sources"][target]["head_sha"] = saved
        self.save_manifest()
        (self.root / "CHROMIUM_VERSION").write_text("153.0.0.0")
        client = mock.Mock()
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "pin_mismatch")
        self.assertIn("sha256", result["manifest"])
        client.json.assert_not_called()

    def test_manifest_trust_and_missing_digest(self):
        for key, value in (("repository", "attacker/repo"), ("head_branch", "untrusted"),
                           ("event", "pull_request"), ("workflow_path", "other.yml")):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.manifest)
                changed["sources"]["windows"][key] = value
                (self.root / "build/upstream-cache.json").write_text(json.dumps(changed))
                with self.assertRaises(cache.CacheMiss):
                    cache.load_manifest("windows", "x64", root=self.root)
        self.manifest["sources"]["windows"]["artifacts"]["x64"]["digest"] = None
        self.save_manifest()
        with self.assertRaisesRegex(cache.CacheMiss, "missing_pinned_digest"):
            cache.load_manifest("windows", "x64", root=self.root)

    def test_manual_run_id_must_match(self):
        cache.load_manifest("windows", "x64", self.pin["run_id"], self.root)
        result = cache.fetch("windows", "x64", self.destination, self.pin["run_id"] + 1, self.root)
        self.assertEqual(result["reason"], "run_id_mismatch")
        self.assertEqual(result["status"], "miss")

    def test_run_and_artifact_provenance(self):
        run, artifact = metadata(self.pin)
        cache.validate_metadata(self.pin, run, artifact, NOW)
        for key, value in (("id", 1), ("head_sha", "0" * 40), ("head_branch", "main"),
                           ("event", "pull_request"), ("path", "other.yml"),
                           ("conclusion", "failure"), ("status", "in_progress")):
            with self.subTest(key=key), self.assertRaises(cache.CacheMiss):
                cache.validate_metadata(self.pin, {**run, key: value}, artifact, NOW)
        for field in ("repository", "head_repository"):
            for key, value in (("id", 1), ("full_name", "attacker/fork"), ("private", True)):
                changed = copy.deepcopy(run)
                changed[field][key] = value
                with self.subTest(field=field, key=key), self.assertRaises(cache.CacheMiss):
                    cache.validate_metadata(self.pin, changed, artifact, NOW)
        for key, value in (("id", 1), ("name", "wrong"), ("digest", digest(b"wrong")),
                           ("size_in_bytes", 1)):
            with self.subTest(key=key), self.assertRaises(cache.CacheMiss):
                cache.validate_metadata(self.pin, run, {**artifact, key: value}, NOW)
        for key in artifact["workflow_run"]:
            changed = copy.deepcopy(artifact)
            changed["workflow_run"][key] = "wrong"
            with self.subTest(workflow=key), self.assertRaises(cache.CacheMiss):
                cache.validate_metadata(self.pin, run, changed, NOW)

    def test_expiry_flag_and_timestamp(self):
        run, artifact = metadata(self.pin)
        for patch in ({"expired": True}, {"expires_at": "2020-01-01T00:00:00Z"},
                      {"expires_at": "2026-09-08T00:00:00Z"}, {"expires_at": None}):
            with self.subTest(patch=patch), self.assertRaises(cache.CacheMiss):
                cache.validate_metadata(self.pin, run, {**artifact, **patch}, NOW)
        client = self.fixture_client()
        run, artifact = client.json.side_effect
        client.json.side_effect = [run, {**artifact, "expired": True}]
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "artifact_expired")
        client.open.assert_not_called()

    def test_missing_and_rate_limited_api_is_bounded_miss(self):
        for status in (401, 403, 404, 410, 429, 503):
            with self.subTest(status=status):
                client = cache.GitHub("fixture")
                client.open = mock.Mock(side_effect=urllib.error.HTTPError(
                    "https://api.github.com", status, "error", {}, None))
                with mock.patch.object(cache.time, "sleep"):
                    result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
                self.assertEqual(result["reason"], f"github_http_{status}")
                self.assertEqual(client.open.call_count, 3 if status in (429, 503) else 1)
                self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_redirect_never_forwards_auth_even_back_to_api(self):
        client = cache.GitHub("secret")
        destinations = ["https://x.blob.core.windows.net/signed?sig=secret",
                        "https://api.github.com/redirected"]
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, cache.TIMEOUT)
            if len(requests) <= len(destinations):
                raise urllib.error.HTTPError(request.full_url, 302, "redirect",
                                             {"Location": destinations[len(requests) - 1]}, None)
            return Response(b"fixture")

        client.opener.open = open_request
        with client.open(cache.API + "/repos/o/r/actions/artifacts/1/zip", download=True):
            pass
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer secret")
        for request in requests[1:]:
            self.assertIsNone(request.get_header("Authorization"))
            self.assertIsNone(request.get_header("Accept"))
        self.assertIsNone(cache.NoRedirect().redirect_request(None, None, 302, "", {}, ""))

    def test_reject_unsafe_redirects_and_metadata_redirects(self):
        for url in ("http://x.blob.core.windows.net/file", "https://evil.example/file",
                    "https://api.github.com.evil.example/file", "file:///tmp/file",
                    "https://user@x.blob.core.windows.net/file", "https://127.0.0.1/file"):
            client = cache.GitHub("secret")
            client.opener.open = mock.Mock(side_effect=urllib.error.HTTPError(
                cache.API, 302, "redirect", {"Location": url}, None))
            with self.subTest(url=url), self.assertRaises(cache.CacheMiss):
                client.open(cache.API + "/artifact", download=True)
            self.assertEqual(client.opener.open.call_count, 1)
        client = cache.GitHub("secret")
        client.opener.open = mock.Mock(side_effect=urllib.error.HTTPError(
            cache.API, 302, "redirect", {"Location": cache.API + "/other"}, None))
        with self.assertRaises(urllib.error.HTTPError):
            client.open(cache.API + "/metadata")
        with mock.patch.dict(os.environ, {"GH_TOKEN": "env-token", "GITHUB_TOKEN": "not-used"}):
            self.assertEqual(cache.GitHub().token, "env-token")

    def test_download_digest_size_and_retry(self):
        data = b"fixture" * 100
        pin = copy.deepcopy(self.pin)
        pin["artifact"].update(digest=digest(data), size_in_bytes=len(data))
        path = self.root / "download"
        client = cache.GitHub("")
        client.open = mock.Mock(side_effect=[Response(data[:4]), Response(data)])
        with mock.patch.object(cache.time, "sleep"):
            self.assertEqual(client.download(pin, path), len(data))
        self.assertEqual(cache.sha256(path), digest(data))
        self.assertEqual(client.open.call_count, 2)
        for bad, reason in ((b"x" * len(data), "checksum_mismatch"),
                            (data + b"x", "download_size_mismatch")):
            client.open = mock.Mock(return_value=Response(bad))
            with self.subTest(reason=reason), self.assertRaisesRegex(cache.CacheMiss, reason):
                client.download(pin, path)
        with mock.patch.object(cache, "DOWNLOAD_SECONDS", -1), self.assertRaisesRegex(
                cache.CacheMiss, "download_timeout"):
            client.download(pin, path)

    def test_bad_hash_never_opens_archive(self):
        client = self.fixture_client()
        client.open = mock.Mock(return_value=Response(b"corrupt"))
        with mock.patch.object(cache.time, "sleep"), mock.patch.object(cache, "unpack_outer") as unpack:
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["status"], "miss")
        unpack.assert_not_called()
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_safe_tar_and_zip_preserve_links_modes_mtime_and_ninja(self):
        entries = [("src", "dir", b""), ("src/bin", "file", b"binary"),
                   ("src/out/.ninja_log", "file", b"log"),
                   ("src/link", "sym", "bin"), ("src/dangling", "sym", "missing"),
                   ("src/node", "sym", "/usr/bin/node")]
        for kind in ("tar", "zip"):
            tree = self.root / kind
            tree.mkdir()
            with self.subTest(kind=kind):
                if kind == "tar":
                    data = tar_bytes(entries + [("src/hard", "hard", "src/bin")])
                    result = cache.extract_tar(io.BytesIO(data), tree)
                    self.assertEqual((tree / "src/bin").stat().st_ino, (tree / "src/hard").stat().st_ino)
                else:
                    result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree)
                self.assertEqual((tree / "src/bin").stat().st_mode & 0o777, 0o755)
                for name in ("src", "src/bin", "src/out/.ninja_log"):
                    self.assertEqual((tree / name).stat().st_mtime_ns, MTIME)
                self.assertEqual(os.readlink(tree / "src/link"), "bin")
                self.assertEqual((tree / "src/link").lstat().st_mtime_ns, MTIME)
                self.assertEqual(os.readlink(tree / "src/dangling"), "missing")
                self.assertFalse((tree / "src/node").is_symlink())
                self.assertEqual(result["skipped_external_symlinks"], 1)

    def test_traversal_and_duplicate_members(self):
        for name in ("../outside", "/outside", "src/../../outside", "C:/outside",
                     "src\\outside", "src/file:stream", "src/NUL", "src/space "):
            for kind in ("tar", "zip"):
                tree = self.root / "unsafe"
                tree.mkdir(exist_ok=True)
                with self.subTest(name=name, kind=kind), self.assertRaises(cache.CacheMiss):
                    entries = [(name, "file", b"bad")]
                    if kind == "tar":
                        cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree)
                    else:
                        cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree)
        entries = [("src/file", "file", b"a"), ("./src/file", "file", b"b")]
        with self.assertRaisesRegex(cache.CacheMiss, "duplicate_archive_path"):
            cache.extract_tar(io.BytesIO(tar_bytes(entries)), self.root / "duplicate")
        self.assertFalse((self.root / "outside").exists())

    def test_escaping_links_chains_parent_writes_and_special_files(self):
        cases = [
            [("src/link", "sym", "../../outside")],
            [("src/link", "hard", "../outside")],
            [("src/link", "hard", "/outside")],
            [("src/link", "sym", "/tmp"), ("src/link/file", "file", b"bad")],
            [("src/up", "sym", ".."), ("src/escape", "sym", "up/../outside")],
            [("src/a", "sym", "b"), ("src/b", "sym", "a")],
            [("src/sym", "sym", "file"), ("src/hard", "hard", "src/sym")],
            [("src/fifo", "fifo", b"")],
        ]
        for index, entries in enumerate(cases):
            tree = self.root / f"links-{index}"
            tree.mkdir()
            with self.subTest(entries=entries), self.assertRaises(cache.CacheMiss):
                cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree)

    def test_outer_requires_exact_inner_and_checks_all_paths(self):
        for entries in ([("wrong.zip", "file", b"bad")],
                        [("artifacts.zip", "file", b"ok"), ("../outside", "file", b"bad")],
                        [("artifacts.zip", "sym", "/tmp/inner")]):
            outer = self.root / "outer.zip"
            outer.write_bytes(zip_bytes(entries))
            with self.subTest(entries=entries), self.assertRaises(cache.CacheMiss):
                cache.unpack_outer(outer, self.root / "inner", "artifacts.zip")
            self.assertFalse((self.root / "inner").exists())

    def test_windows_hit_both_shapes_cleanup_and_idempotence(self):
        for source in ("src", "build/src"):
            with self.subTest(source=source):
                client = self.fixture_client(source=source)
                destination = self.root / source.replace("/", "-")
                result = cache.fetch("windows", "x64", destination, root=self.root, client=client)
                self.assertEqual(result["status"], "hit", result)
                self.assertEqual(result["source"], str(destination / "tree" / source))
                self.assertEqual(result["manifest"]["artifact_id"], self.pin["artifact"]["id"])
                self.assertGreater(result["download_bytes"], 0)
                self.assertGreater(result["inner_bytes"], 0)
                self.assertGreater(result["extracted_bytes"], 0)
                self.assertEqual({p.name for p in destination.iterdir()}, {"result.json", "tree"})
                second = cache.fetch("windows", "x64", destination, root=self.root, client=mock.Mock())
                self.assertEqual(result, second)
                self.assertEqual(result["extraction_scope"], "source-and-objects")
                source_path = Path(result["source"])
                expected = {"chrome/browser/file.cc": b"source", "out/Default/args.gn": b'target_cpu="x64"',
                            "out/Default/build.ninja": b"build-ninja", "out/Default/obj/file.o": b"object",
                            "out/Default/gen/generated.h": b"generated", "out/Default/.ninja_log": b"ninja-log",
                            "out/Default/.ninja_deps": b"ninja-deps"}
                for name, content in expected.items():
                    self.assertEqual((source_path / name).read_bytes(), content)
                    self.assertEqual((source_path / name).stat().st_mtime_ns, MTIME)
                self.assertFalse((destination / "tree/build/download_cache").exists())

    @unittest.skipUnless(shutil.which("zstd"), "host zstd is unavailable")
    def test_linux_and_macos_zstd_hits(self):
        for platform, arch in (("linux", "x64"), ("linux", "arm64"), ("macos", "x64"), ("macos", "arm64")):
            with self.subTest(platform=platform, arch=arch):
                source = "build/src" if platform == "linux" else "src"
                retained = ["chrome/browser/file.cc", "out/Default/build.ninja",
                            "out/Default/obj/file.o", "out/Default/gen/generated.h"]
                extra = [(source + "/third_party/node/linux/node-linux-x64/bin/node", "sym", "/usr/bin/node")]
                client = self.fixture_client(platform, arch, source, extra)
                destination = self.root / f"{platform}-{arch}"
                result = cache.fetch(platform, arch, destination, root=self.root, client=client)
                self.assertEqual(result["status"], "hit", result)
                self.assertTrue(Path(result["source"]).is_absolute())
                self.assertEqual(result["extraction_scope"], "source-and-objects")
                for name in retained + ["out/Default/.ninja_log", "out/Default/.ninja_deps"]:
                    self.assertTrue((Path(result["source"]) / name).is_file(), name)
                    self.assertEqual((Path(result["source"]) / name).stat().st_mtime_ns, MTIME)
                self.assertFalse((destination / "tree/build/download_cache").exists())
                self.assertFalse((Path(result["source"]) / "third_party/node").exists())
                self.assertEqual(result["skipped_external_symlinks"], 1)
                offline = mock.Mock()
                second = cache.fetch(platform, arch, destination, root=self.root, client=offline)
                self.assertEqual(result, second)
                offline.json.assert_not_called()
                offline.download.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX archive names required")
    def test_linux_sysroot_preserves_literal_systemd_backslash(self):
        name = r"build/src/build/linux/sysroot/lib/systemd/system/system-systemd\x2dcryptsetup.slice"
        tree = self.root / "systemd"
        tree.mkdir()
        cache.extract_tar(io.BytesIO(tar_bytes([(name, "file", b"unit")])), tree,
                          cache.SourceSelection(["build/src"]))
        self.assertEqual((tree / name).read_bytes(), b"unit")
        with self.assertRaises(cache.CacheMiss):
            cache.safe_name(name)
        with self.assertRaises(cache.CacheMiss):
            cache.safe_name("build/src/../../escape", posix=True)

    @unittest.skipUnless(os.name == "posix", "POSIX archive names required")
    def test_posix_source_paths_and_link_targets_preserve_literal_names(self):
        names = [r"system-systemd\x2dcryptsetup.slice", "name:with:colon", "trailing.",
                 "trailing ", "NUL", 'literal<>"|?*']
        for platform in ("linux", "macos"):
            for kind in ("tar", "zip"):
                with self.subTest(platform=platform, kind=kind):
                    tree = self.root / f"literal-{platform}-{kind}"
                    tree.mkdir()
                    entries = [("src/" + name, "file", b"literal") for name in names]
                    entries += [(f"src/link-{index}", "sym", name) for index, name in enumerate(names)]
                    selection = cache.SourceSelection(["src"], platform=platform)
                    if kind == "tar":
                        entries += [(f"src/hard-{index}", "hard", "src/" + name)
                                    for index, name in enumerate(names)]
                        cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                        for index, name in enumerate(names):
                            self.assertEqual((tree / "src" / name).stat().st_ino,
                                             (tree / f"src/hard-{index}").stat().st_ino)
                    else:
                        cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                    for index, name in enumerate(names):
                        self.assertEqual((tree / "src" / name).read_bytes(), b"literal")
                        self.assertEqual((tree / f"src/link-{index}").read_bytes(), b"literal")
                        self.assertEqual(os.readlink(tree / f"src/link-{index}"), name)

    def test_windows_source_paths_and_link_targets_remain_strict(self):
        names = [r"literal\x2dname", r"..\outside", r"C:\outside", r"\\host\share", "C:/outside",
                 "file:stream", "NUL", "NUL.txt", "COM¹", "CONOUT$", "trailing.", "trailing ",
                 "bad<name", "bad>name", 'bad"name', "bad|name", "bad?name", "bad*name"]
        for kind in ("tar", "zip"):
            for index, name in enumerate(names):
                for member in ("file", "sym", "hard") if kind == "tar" else ("file", "sym"):
                    with self.subTest(kind=kind, name=name, member=member):
                        tree = self.root / f"strict-{kind}-{index}-{member}"
                        tree.mkdir()
                        entry = ("src/" + name, "file", b"bad") if member == "file" else (
                            "src/link", member, "src/" + name if member == "hard" else name)
                        selection = cache.SourceSelection(["src"], platform="windows")
                        with self.assertRaises(cache.CacheMiss):
                            if kind == "tar":
                                cache.extract_tar(io.BytesIO(tar_bytes([entry])), tree, selection)
                            else:
                                cache.extract_zip(io.BytesIO(zip_bytes([entry])), tree, selection)
        with mock.patch.object(cache.os, "name", "nt"), self.assertRaises(cache.CacheMiss):
            cache.safe_name(r"src/literal\x2dname", posix=True)
        client = self.fixture_client(extra=[(r"src/literal\x2dname", "file", b"bad")])
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "unsafe_archive_path")
        self.assertEqual(result["extraction_scope"], "source-and-objects")
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_case_folding_targets_reject_collisions_in_members_parents_and_links(self):
        cases = [
            [("src/File", "file", b"a"), ("src/file", "file", b"b")],
            [("src/Dir/a", "file", b"a"), ("src/dir/b", "file", b"b")],
            [("src/Dir", "dir", b""), ("src/dir", "sym", ".")],
            [("src/Dir/a", "file", b"a"), ("src/dir", "dir", b"")],
            [("src/é", "file", b"a"), ("src/e\u0301", "file", b"b")],
            [("src/A", "sym", "/outside"), ("src/link", "sym", "a")],
            [("src/A", "sym", "a")],
            [("src/A", "sym", "B"), ("src/b", "sym", "a")],
            [("src/A", "sym", ".."), ("src/link", "sym", "a/../outside")],
            [("foreign/Dir/a", "file", b"a"), ("foreign/dir/b", "file", b"b")],
        ]
        for platform in ("windows", "macos"):
            for kind in ("tar", "zip"):
                for index, entries in enumerate(cases):
                    with self.subTest(platform=platform, kind=kind, entries=entries):
                        tree = self.root / f"case-{platform}-{kind}-{index}"
                        tree.mkdir()
                        selection = cache.SourceSelection(["src"], platform=platform)
                        with self.assertRaises(cache.CacheMiss):
                            if kind == "tar":
                                cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                            else:
                                cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                tree = self.root / f"case-hard-{platform}-{kind}"
                tree.mkdir()
                entries = [("src/File", "file", b"a"), ("src/hard", "hard", "src/file")]
                with self.assertRaisesRegex(cache.CacheMiss, "archive_case_collision"):
                    cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree,
                                      cache.SourceSelection(["src"], platform=platform))

    def test_framework_and_forward_directory_symlink_chains(self):
        framework = "src/out/Default/Chromium Framework.framework"
        entries = [(framework + "/Resources", "sym", "Versions/Current/Resources"),
                   (framework + "/Versions/Current", "sym", "A"),
                   (framework + "/Versions/A/Resources/data", "file", b"resource"),
                   (framework + "/Chromium Framework", "sym", "Versions/Current/Chromium Framework"),
                   (framework + "/Versions/A/Chromium Framework", "file", b"binary"),
                   ("src/forward", "sym", "directory-link"),
                   ("src/directory-link", "sym", "empty"), ("src/empty", "dir", b""),
                   ("src/file-link", "sym", "hard-link"),
                   ("src/dangling", "sym", "missing")]
        for platform in ("windows", "macos"):
            for kind in ("tar", "zip"):
                with self.subTest(platform=platform, kind=kind):
                    tree = self.root / f"framework-{platform}-{kind}"
                    tree.mkdir()
                    selection = cache.SourceSelection(["src"], platform=platform)
                    with mock.patch.object(cache.os, "symlink", wraps=os.symlink) as symlink:
                        if kind == "tar":
                            data = tar_bytes(entries + [("src/hard-link", "hard",
                                                         framework + "/Versions/A/Chromium Framework")])
                            cache.extract_tar(io.BytesIO(data), tree, selection)
                        else:
                            data = zip_bytes(entries + [("src/hard-link", "file", b"binary")])
                            cache.extract_zip(io.BytesIO(data), tree, selection)
                    directories = {framework + "/Resources", framework + "/Versions/Current",
                                   "src/forward", "src/directory-link"}
                    for call in symlink.call_args_list:
                        name = call.args[1].relative_to(tree).as_posix()
                        self.assertEqual(call.kwargs["target_is_directory"], name in directories, name)
                    self.assertEqual((tree / framework / "Resources/data").read_bytes(), b"resource")
                    self.assertEqual((tree / framework / "Chromium Framework").read_bytes(), b"binary")
                    self.assertTrue((tree / "src/forward").is_dir())
                    self.assertEqual((tree / "src/file-link").read_bytes(), b"binary")
                    self.assertEqual(os.readlink(tree / "src/dangling"), "missing")

    def test_corrupt_archives_and_limits_fall_back(self):
        client = self.fixture_client()
        with mock.patch.object(cache, "extract_inner", side_effect=zipfile.BadZipFile("bad")):
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["status"], "miss")
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})
        tree = self.root / "limited"
        tree.mkdir()
        with mock.patch.object(cache, "MAX_EXTRACTED", 2), self.assertRaisesRegex(
                cache.CacheMiss, "archive_too_large"):
            cache.extract_tar(io.BytesIO(tar_bytes([("src/file", "file", b"large")])), tree)
        if shutil.which("zstd"):
            inner = self.root / "invalid.zst"
            inner.write_bytes(b"not zstd")
            with self.assertRaises((cache.CacheMiss, tarfile.TarError)):
                cache.extract_inner(inner, self.root / "bad-zstd", shutil.which("zstd"))

    def test_result_owned_stale_tree_cleanup_and_lock(self):
        client = self.fixture_client()
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["status"], "hit")
        (self.destination / ".lock").write_text("")
        with self.assertRaises(cache.LocalError):
            cache.fetch("windows", "x64", self.destination, root=self.root)
        self.assertTrue(Path(result["source"]).is_dir())
        (self.destination / ".lock").unlink()
        (self.root / "CHROMIUM_VERSION").write_text("0.0.0.0")
        result = cache.fetch("windows", "x64", self.destination, root=self.root)
        self.assertEqual(result["status"], "miss")
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_source_shape_and_version_fallback(self):
        client = self.fixture_client(source="wrong")
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "invalid_source_shape")
        client = self.fixture_client()
        with mock.patch.object(cache, "source_path", side_effect=cache.CacheMiss("source_version_mismatch")):
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "source_version_mismatch")
        tree = self.root / "version-tree"
        (tree / "src/chrome").mkdir(parents=True)
        (tree / "src/BUILD.gn").write_text("fixture")
        (tree / "src/chrome/VERSION").write_text("MAJOR=0\nMINOR=0\nBUILD=0\nPATCH=0\n")
        with self.assertRaisesRegex(cache.CacheMiss, "source_version_mismatch"):
            cache.source_path(tree, self.pin)

    def test_local_destination_and_cli_failure(self):
        self.destination.mkdir()
        sentinel = self.destination / "do-not-delete"
        sentinel.write_text("important")
        with self.assertRaises(cache.LocalError):
            cache.fetch("windows", "x64", self.destination)
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(cache.main(["--platform", "windows", "--arch", "x64",
                                         "--destination", str(self.destination)]), 2)
        self.assertEqual(sentinel.read_text(), "important")
        link = self.root / "link"
        link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(cache.LocalError):
            cache.destination_path(str(link / "cache"))
        with self.assertRaises(cache.LocalError):
            cache.destination_path("")
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as error:
            cache.main(["--platform", "windows", "--arch", "x64", "--destination", str(self.root), "--run-id", "0"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_miss_exit_zero_structured_output_and_foreign_result(self):
        with mock.patch("sys.stdout", new=io.StringIO()) as output, mock.patch("sys.stderr", new=io.StringIO()):
            code = cache.main(["--platform", "windows", "--arch", "arm64", "--destination", str(self.destination)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "miss")
        self.assertEqual(json.loads((self.destination / "result.json").read_text())["status"], "miss")
        (self.destination / "result.json").write_text('{"status":"hit"}')
        with self.assertRaises(cache.LocalError):
            cache.destination_path(str(self.destination))

    def test_selected_tar_and_zip_keep_only_toolchains_and_metadata(self):
        llvm = "third_party/llvm-build/Release+Asserts"
        retained = ["BUILD.gn", "chrome/VERSION", "out/Default/args.gn", "tools/clang/scripts/update.py",
                    "tools/rust/update_rust.py", llvm + "/bin/clang", "third_party/rust-toolchain/bin/rustc"]
        excluded = ["chrome/browser/file.cc", "out/Default/obj/file.o", "out/Default/.ninja_log",
                    "out/Default/.ninja_deps", "tools/clang/__pycache__/update.pyc", "tools/rust/old.pyc",
                    "third_party/llvm-build/Debug/bin/clang", "third_party/rust-toolchain-backup/bin/rustc",
                    "download_cache/package.tar.xz"]
        for kind in ("tar", "zip"):
            for source in ("src", "build/src"):
                with self.subTest(kind=kind, source=source):
                    tree = self.root / (kind + source.replace("/", "-"))
                    tree.mkdir()
                    entries = [(source + "/" + name, "file", b"fixture") for name in retained + excluded]
                    entries += [(source + "/" + llvm, "dir", b""),
                                (source + "/" + llvm + "/bin/clang++", "sym", "clang"),
                                ("foreign/src/tools/clang/scripts/update.py", "file", b"excluded")]
                    if kind == "tar":
                        entries.append((source + "/" + llvm + "/bin/clang-hard", "hard",
                                        source + "/" + llvm + "/bin/clang"))
                        result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree,
                                                   cache.ToolchainSelection([source]))
                    else:
                        result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree,
                                                   cache.ToolchainSelection([source]))
                    for name in retained:
                        path = tree / source / name
                        self.assertEqual(path.read_bytes(), b"fixture")
                        self.assertEqual(path.stat().st_mtime_ns, MTIME)
                        self.assertEqual(path.stat().st_mode & 0o777, 0o755)
                    for name in excluded:
                        self.assertFalse((tree / source / name).exists(), name)
                    self.assertFalse((tree / "foreign").exists())
                    link = tree / source / llvm / "bin/clang++"
                    self.assertEqual(os.readlink(link), "clang")
                    self.assertEqual(link.lstat().st_mtime_ns, MTIME)
                    self.assertEqual((tree / source / llvm).stat().st_mtime_ns, MTIME)
                    self.assertEqual(result["extracted_bytes"], len(retained) * len(b"fixture"))

    def test_source_selection_keeps_full_snapshot_within_pinned_roots(self):
        retained = ["BUILD.gn", "chrome/VERSION", "chrome/browser/file.cc", "base/header.h",
                    "out/Default/args.gn", "out/Default/build.ninja", "out/Default/toolchain.ninja",
                    "out/Default/.ninja_log", "out/Default/.ninja_deps", "out/Default/obj/file.o",
                    "out/Default/gen/generated.h", "tools/clang/scripts/update.py",
                    "third_party/llvm-build/Release+Asserts/bin/clang",
                    "third_party/llvm-build-tools/include/header.h", "third_party/rust-src/library/lib.rs",
                    "third_party/rust-toolchain/lib/rustlib/src/rust/library/lib.rs",
                    "tools/clang/__pycache__/update.pyc", "download_cache/source-fixture"]
        excluded = ["build/download_cache/package.tar.xz", "build/other/data", "download_cache/package.tar.xz",
                    "build/src-backup/base/header.h", "foreign/build/src/base/header.h", "src/base/header.h"]
        source = "build/src"
        selection = cache.SourceSelection([source])
        self.assertTrue(selection("build", "dir"))
        self.assertFalse(selection("build"))
        for kind in ("tar", "zip"):
            with self.subTest(kind=kind):
                tree = self.root / f"full-source-{kind}"
                tree.mkdir()
                entries = [("build", "dir", b""), (source, "dir", b""),
                           (source + "/out/Default/obj", "dir", b""),
                           ("build/download_cache", "dir", b""),
                           (source + "/empty", "dir", b"")]
                entries += [(source + "/" + name, "file", b"fixture") for name in retained]
                entries += [(name, "file", b"excluded") for name in excluded]
                entries += [(source + "/out/Default/gen/header-link.h", "sym", "../../../base/header.h"),
                            (source + "/third_party/llvm-build/Release+Asserts/bin/clang++", "sym", "clang")]
                if kind == "tar":
                    entries.append((source + "/out/Default/obj/hard.o", "hard", source + "/out/Default/obj/file.o"))
                    result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                    self.assertEqual((tree / source / "out/Default/obj/file.o").stat().st_ino,
                                     (tree / source / "out/Default/obj/hard.o").stat().st_ino)
                else:
                    result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                for name in retained:
                    path = tree / source / name
                    self.assertEqual(path.read_bytes(), b"fixture")
                    self.assertEqual(path.stat().st_mtime_ns, MTIME)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o755)
                for name in excluded + ["build/download_cache", "build/other", "foreign", "build/src-backup"]:
                    self.assertFalse((tree / name).exists(), name)
                for name in ("build", source, source + "/out/Default/obj", source + "/empty"):
                    self.assertEqual((tree / name).stat().st_mtime_ns, MTIME)
                self.assertEqual((tree / source / "out/Default/gen/header-link.h").read_bytes(), b"fixture")
                self.assertEqual(result["extracted_bytes"], len(retained) * len(b"fixture"))

    def test_source_selection_accepts_multiple_roots_and_rejects_unsafe_roots(self):
        selection = cache.SourceSelection(["src", "build/src"])
        for name in ("src", "src/out/Default/obj/file.o", "build/src/base/header.h"):
            self.assertTrue(selection(name), name)
        for name in ("src-backup/file", "build/src-backup/file", "build/download_cache/file", "foreign/src/file"):
            self.assertFalse(selection(name), name)
        for roots in ([], [""], ["."], ["/src"], ["../src"], ["build/../src"]):
            with self.subTest(roots=roots), self.assertRaises(cache.CacheMiss):
                cache.SourceSelection(roots)

    def test_source_selection_skips_absolute_system_links(self):
        names = ["build/src/third_party/node/linux/node-linux-x64/bin/node",
                 "build/src/third_party/llvm-build/Release+Asserts/bin/system-tool",
                 "build/src/tools/clang/system-tool", "build/download_cache/node"]
        entries = [(name, "sym", "/usr/bin/node") for name in names]
        for kind in ("tar", "zip"):
            with self.subTest(kind=kind):
                tree = self.root / f"system-links-{kind}"
                tree.mkdir()
                selection = cache.SourceSelection(["build/src"])
                if kind == "tar":
                    result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                else:
                    result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                self.assertEqual(result["skipped_external_symlinks"], len(names))
                self.assertEqual(result["extracted_bytes"], 0)
                for name in names:
                    self.assertFalse((tree / name).is_symlink())
                self.assertEqual(list(tree.iterdir()), [])

    def test_source_selection_remaps_known_platform_internal_absolute_links(self):
        roots = [
            ("linux", "build/src", "/repo/build/src"),
            ("macos", "src", "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src"),
            ("windows", "src", r"C:\ungoogled-chromium-windows\build\src"),
            ("windows", "build/src", "C:/ungoogled-chromium-windows/build/src"),
        ]
        rust = "third_party/rust-toolchain"
        llvm = "third_party/llvm-build/Release+Asserts/bin"
        files = {rust + "/rustc/bin/rustc": b"rustc", rust + "/rustfmt-preview/bin/rustfmt": b"rustfmt",
                 rust + "/cargo/bin/cargo": b"cargo", rust + "/rustc/lib/libLLVM.dylib": b"lib",
                 llvm + "/llvm-install-name-tool": b"llvm"}
        links = {rust + "/bin/rustc": rust + "/rustc/bin/rustc",
                 rust + "/bin/rustfmt": rust + "/rustfmt-preview/bin/rustfmt",
                 rust + "/bin/cargo": rust + "/cargo/bin/cargo",
                 rust + "/lib/libLLVM.dylib": rust + "/rustc/lib/libLLVM.dylib",
                 rust + "/lib/libLLVM-current.dylib": rust + "/lib/libLLVM.dylib",
                 rust + "/lib/current": rust + "/lib/runtime",
                 rust + "/lib/runtime": rust + "/rustc/lib",
                 llvm + "/install_name_tool": llvm + "/llvm-install-name-tool"}
        for platform, source, original in roots:
            separator = "\\" if "\\" in original else "/"
            for kind in ("tar", "zip"):
                with self.subTest(platform=platform, source=source, kind=kind):
                    tree = self.root / f"absolute-internal-{platform}-{source.replace('/', '-')}-{kind}"
                    tree.mkdir()
                    entries = [(source + "/" + name, "sym", original + separator + target.replace("/", separator))
                               for name, target in links.items()]
                    entries += [(source + "/" + name, "file", data) for name, data in files.items()]
                    entries.append((source + "/" + rust + "/bin/rustc-relative", "sym", "rustc"))
                    selection = cache.SourceSelection(cache.SOURCES[platform][2], platform=platform)
                    if kind == "tar":
                        result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                    else:
                        result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                    self.assertEqual(result["remapped_internal_symlinks"], len(links))
                    self.assertEqual(result["skipped_external_symlinks"], 0)
                    self.assertEqual(result["extracted_bytes"], sum(map(len, files.values())))
                    for name, target in links.items():
                        link = tree / source / name
                        relative = os.path.relpath(tree / source / target, link.parent).replace(os.sep, "/")
                        self.assertEqual(os.readlink(link), relative)
                        self.assertTrue(link.resolve().is_relative_to(tree / source))
                        self.assertEqual(link.lstat().st_mtime_ns, MTIME)
                    for name, content in ((rust + "/bin/rustc", b"rustc"), (rust + "/bin/rustfmt", b"rustfmt"),
                                          (rust + "/bin/cargo", b"cargo"), (rust + "/bin/rustc-relative", b"rustc"),
                                          (rust + "/lib/libLLVM-current.dylib", b"lib"),
                                          (rust + "/lib/current/libLLVM.dylib", b"lib"),
                                          (llvm + "/install_name_tool", b"llvm")):
                        self.assertEqual((tree / source / name).read_bytes(), content)

    def test_absolute_external_links_are_omitted_and_unknown_internal_links_reject(self):
        roots = {
            "linux": ("build/src", "/repo/build/src"),
            "macos": ("src", "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src"),
            "windows": ("src", r"C:\ungoogled-chromium-windows\build\src"),
        }
        for platform, (source, original) in roots.items():
            for kind in ("tar", "zip"):
                with self.subTest(platform=platform, kind=kind):
                    tree = self.root / f"absolute-external-{platform}-{kind}"
                    tree.mkdir()
                    entries = [
                        (f"{source}/sdk-link", "sym", "/Applications/Xcode.app/Contents/Developer/SDKs/MacOSX.sdk/usr/include/stdio.h"),
                        (f"{source}/go-link", "sym", "/usr/local/go/bin/go"),
                    ]
                    selection = cache.SourceSelection([source], platform=platform)
                    if kind == "tar":
                        result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                    else:
                        result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                    self.assertEqual(result["remapped_internal_symlinks"], 0)
                    self.assertEqual(result["skipped_external_symlinks"], 2)
                    self.assertEqual(set(result["external_symlink_paths"]),
                                     {f"{source}/sdk-link", f"{source}/go-link"})
                    self.assertFalse((tree / source / "sdk-link").exists())
                    self.assertFalse((tree / source / "go-link").exists())

                    unknown = [(f"{source}/missing", "sym", original + "/not-in-archive")]
                    bad_tree = self.root / f"absolute-missing-{platform}-{kind}"
                    bad_tree.mkdir()
                    with self.assertRaisesRegex(cache.CacheMiss, "missing_internal_symlink_target"):
                        if kind == "tar":
                            cache.extract_tar(io.BytesIO(tar_bytes(unknown)), bad_tree, selection)
                        else:
                            cache.extract_zip(io.BytesIO(zip_bytes(unknown)), bad_tree, selection)

        tree = self.root / "linux-rejects-macos-root"
        tree.mkdir()
        mac_target = ("/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/"
                      "build/src/third_party/rust-toolchain/rustc/bin/rustc")
        result = cache.extract_tar(io.BytesIO(tar_bytes([("build/src/rustc", "sym", mac_target)])), tree,
                                   cache.SourceSelection(["build/src"], platform="linux"))
        self.assertEqual(result["remapped_internal_symlinks"], 0)
        self.assertEqual(result["skipped_external_symlinks"], 1)
        self.assertFalse((tree / "build/src/rustc").exists())

    def test_remapped_absolute_links_still_validate_cycles_escape_case_and_existence(self):
        roots = [("linux", "build/src", "/repo/build/src"),
                 ("macos", "src", "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src"),
                 ("windows", "src", r"C:\ungoogled-chromium-windows\build\src")]
        for platform, source, original in roots:
            cases = [
                [("a", "sym", original + "/a")],
                [("a", "sym", original + "/b"), ("b", "sym", original + "/a")],
                [("a", "sym", original + "/b"), ("b", "sym", "a")],
                [("a", "sym", original + "/../download_cache/file")],
                [("a", "sym", original + "/b"), ("b", "sym", "../../../outside")],
                [("a", "sym", original + "/b"), ("b", "sym", "../download_cache/file")],
                [("a", "sym", original + "/b"), ("b", "sym", "/Applications/Xcode.app/SDK")],
                [("a", "sym", original + "/missing")],
                [("a", "sym", original + "/b"), ("b", "sym", "missing")],
                [("a", "sym", original + "/b"), ("b", "sym", "missing/../file"), ("file", "file", b"data")],
                [("a", "sym", original + "/b"), ("b", "sym", "file/../file"), ("file", "file", b"data")],
                [("a", "sym", original + "/b"), ("b", "file", b"data"), ("a/nested", "file", b"bad")],
                [("a/nested", "file", b"bad"), ("a", "sym", original + "/b"), ("b", "file", b"data")],
            ]
            if platform in ("macos", "windows"):
                cases += [[("a", "sym", original + "/file"), ("File", "file", b"data")],
                          [("a", "sym", original + "/dir/file"), ("Dir/file", "file", b"data")],
                          [("A", "sym", original + "/a")]]
            if platform == "windows":
                cases += [[("a", "sym", original + suffix)] for suffix in
                          (r"\..\outside", r"\file:stream", r"\NUL", r"\trailing.", r"\bad*name")]
            for kind in ("tar", "zip"):
                for index, members in enumerate(cases):
                    entries = [(source + "/" + name, member, target) for name, member, target in members]
                    tree = self.root / f"remap-unsafe-{platform}-{kind}-{index}"
                    tree.mkdir()
                    with self.subTest(platform=platform, kind=kind, entries=entries), self.assertRaises(cache.CacheMiss):
                        selection = cache.SourceSelection(cache.SOURCES[platform][2], platform=platform)
                        if kind == "tar":
                            cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                        else:
                            cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)

    def test_absolute_root_remapping_is_platform_and_source_scoped(self):
        mac_root = "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src"
        cases = [(cache.SourceSelection(["build/src"], platform="linux"), "build/src", mac_root),
                 (cache.SourceSelection(["src"], platform="windows"), "src", mac_root),
                 (cache.SourceSelection(["src"]), "src", mac_root),
                 (cache.SourceSelection(["foreign/src"], platform="macos"), "foreign/src", mac_root),
                 (cache.SourceSelection(["src"], platform="macos"), "src", mac_root + "-backup"),
                 (cache.SourceSelection(["src"], platform="macos"), "src", "/another/build/src"),
                 (cache.SourceSelection(["src"], platform="macos"), "src", "/repo/build/src")]
        for kind in ("tar", "zip"):
            for index, (selection, source, original) in enumerate(cases):
                tree = self.root / f"remap-scope-{kind}-{index}"
                tree.mkdir()
                entries = [(source + "/file", "file", b"data"), (source + "/link", "sym", original + "/file")]
                with self.subTest(kind=kind, source=source, original=original):
                    if kind == "tar":
                        result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                    else:
                        result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                    self.assertEqual(result["remapped_internal_symlinks"], 0)
                    self.assertEqual(result["external_symlink_paths"], [source + "/link"])
                    self.assertEqual(result["skipped_external_symlinks"], 1)
                    self.assertFalse((tree / source / "link").is_symlink())
                    self.assertEqual((tree / source / "file").read_bytes(), b"data")

    @unittest.skipUnless(shutil.which("zstd"), "host zstd is unavailable")
    def test_macos_fetch_remaps_rust_chain_and_records_sdk_go_omissions(self):
        original = "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src"
        rust = "third_party/rust-toolchain"
        sdk = "src/out/Default/sdk/xcode_links/MacOSX.sdk"
        go = "src/third_party/go/src"
        extra = [("src/" + rust + "/bin/rustc", "sym", original + "/" + rust + "/rustc/bin/rustc"),
                 ("src/" + rust + "/rustc/bin/rustc", "file", b"rustc"),
                 ("src/" + rust + "/bin/rustc-alias", "sym", original + "/" + rust + "/bin/rustc"),
                 (sdk, "sym", "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"),
                 (go, "sym", "/usr/local/go/src")]
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                destination = self.root / f"macos-links-{arch}"
                client = self.fixture_client("macos", arch, "src", extra)
                result = cache.fetch("macos", arch, destination, root=self.root, client=client)
                self.assertEqual(result["status"], "hit", result)
                self.assertEqual(result["remapped_internal_symlinks"], 2)
                self.assertEqual(result["skipped_external_symlinks"], 2)
                self.assertEqual(set(result["external_symlink_paths"]), {sdk, go})
                self.assertEqual((Path(result["source"]) / rust / "bin/rustc-alias").read_bytes(), b"rustc")
                self.assertEqual(json.loads((destination / "result.json").read_text()), result)
                for name in (sdk, go):
                    self.assertFalse((destination / "tree" / name).is_symlink())
        client = self.fixture_client("macos", "x64", "src", extra=[
            ("src/" + rust + "/bin/rustc", "sym", original + "/" + rust + "/rustc/bin/rustc")])
        result = cache.fetch("macos", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "missing_internal_symlink_target")
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_source_selection_rejects_escaping_and_excluded_link_targets(self):
        cases = [
            [("build/src/link", "sym", "../../../outside")],
            [("build/src/link", "sym", "../download_cache/file")],
            [("build/src/link", "sym", "..")],
            [("build/src/node", "sym", "/usr/bin/node"), ("build/src/link", "sym", "node")],
            [("build/src/a", "sym", "b"), ("build/src/b", "sym", "a")],
            [("build/src/node", "sym", "/usr/bin/node"), ("build/src/node/file", "file", b"bad")],
            [("build/src/up", "sym", "."), ("build/src/link", "sym", "up/../../../outside")],
        ]
        for kind in ("tar", "zip"):
            for index, entries in enumerate(cases):
                with self.subTest(kind=kind, entries=entries), self.assertRaises(cache.CacheMiss):
                    tree = self.root / f"source-links-{kind}-{index}"
                    tree.mkdir()
                    selection = cache.SourceSelection(["build/src"])
                    if kind == "tar":
                        cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                    else:
                        cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
        entries = [("build/download_cache/file", "file", b"excluded"),
                   ("build/src/link", "hard", "build/download_cache/file")]
        tree = self.root / "source-hardlink"
        tree.mkdir()
        with self.assertRaisesRegex(cache.CacheMiss, "excluded_link_target"):
            cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, cache.SourceSelection(["build/src"]))

    def test_selection_still_validates_skipped_paths_types_and_links(self):
        cases = [
            [("foreign/../escape", "file", b"bad")],
            [("foreign/link", "sym", "../../escape")],
            [("foreign/link", "sym", "/tmp"), ("foreign/link/file", "file", b"bad")],
            [("foreign/file", "file", b"bad"), ("foreign/file/nested", "file", b"bad")],
            [("foreign/a", "sym", "b"), ("foreign/b", "sym", "a")],
        ]
        cases += [[("build/download_cache/../escape", "file", b"bad")],
                  [("build/download_cache/file", "file", b"a"),
                   ("./build/download_cache/file", "file", b"b")]]
        for selection_type in (cache.ToolchainSelection, cache.SourceSelection):
            selection = selection_type(["build/src"])
            for kind in ("tar", "zip"):
                for index, entries in enumerate(cases):
                    tree = self.root / f"skipped-{selection_type.__name__}-{kind}-{index}"
                    tree.mkdir()
                    with self.subTest(selection=selection_type.__name__, kind=kind, entries=entries):
                        with self.assertRaises(cache.CacheMiss):
                            if kind == "tar":
                                cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                            else:
                                cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                        self.assertEqual(list(tree.iterdir()), [])
            for index, entries in enumerate(([("foreign/link", "hard", "../escape")],
                                             [("foreign/fifo", "fifo", b"")])):
                tree = self.root / f"special-{selection_type.__name__}-{index}"
                tree.mkdir()
                with self.assertRaises(cache.CacheMiss):
                    cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                info = zipfile.ZipInfo("foreign/fifo")
                info.external_attr = (stat.S_IFIFO | 0o644) << 16
                archive.writestr(info, b"")
            with self.assertRaises(cache.CacheMiss):
                cache.extract_zip(io.BytesIO(output.getvalue()), self.root, selection)

    def test_selected_links_cannot_target_excluded_content(self):
        for kind in ("tar", "zip"):
            targets = [("../../chrome/browser/file.cc", []), ("/usr/bin/node", []),
                       ("__pycache__/update.pyc", []),
                       ("../../foreign-link", [("src/foreign-link", "sym", "tools/clang/update.py")])]
            for index, (target, extra) in enumerate(targets):
                tree = self.root / f"excluded-{kind}-{index}"
                tree.mkdir()
                entries = [("src/tools/clang/link", "sym", target), *extra]
                with self.subTest(kind=kind, target=target), self.assertRaisesRegex(
                        cache.CacheMiss, "excluded_link_target"):
                    if kind == "tar":
                        cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, cache.ToolchainSelection(["src"]))
                    else:
                        cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, cache.ToolchainSelection(["src"]))
        entries = [("src/out/Default/obj/file.o", "file", b"object"),
                   ("src/tools/clang/link", "hard", "src/out/Default/obj/file.o")]
        tree = self.root / "excluded-hard"
        tree.mkdir()
        with self.assertRaisesRegex(cache.CacheMiss, "excluded_link_target"):
            cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, cache.ToolchainSelection(["src"]))

    def test_selection_skips_payload_and_enforces_both_byte_limits(self):
        tree = self.root / "limits"
        tree.mkdir()
        extractor = cache.Extractor(tree, cache.ToolchainSelection(["src"]))
        stream = mock.Mock()
        extractor.add("src/out/Default/obj/file.o", "file", 100, 0o644, MTIME, stream)
        stream.read.assert_not_called()
        self.assertEqual(extractor.bytes, 0)
        self.assertEqual(extractor.archive_bytes, 100)
        with mock.patch.object(cache, "MAX_EXTRACTED", 100), self.assertRaisesRegex(
                cache.CacheMiss, "archive_too_large"):
            extractor.add("foreign/file", "file", 1, 0o644, MTIME, stream)
        with mock.patch.object(cache, "MAX_SELECTED", 2), self.assertRaisesRegex(
                cache.CacheMiss, "archive_too_large"):
            extractor.add("src/tools/rust/update.py", "file", 3, 0o644, MTIME, io.BytesIO(b"abc"))
        self.assertEqual(cache.MAX_SELECTED, 30 * 1024**3)
        self.assertEqual(cache.DOWNLOAD_SECONDS, 15 * 60)

    def test_source_selection_uses_full_limit_and_disk_headroom(self):
        for platform in ("linux", "macos", "windows"):
            with self.subTest(platform=platform):
                tree = self.root / f"source-limits-{platform}"
                tree.mkdir()
                extractor = cache.Extractor(tree, cache.SourceSelection(["build/src"], platform=platform))
                stream = mock.Mock()
                self.disk_usage.return_value = mock.Mock(free=1024**4)
                extractor.add("build/download_cache/file", "file", 100, 0o644, MTIME, stream)
                stream.read.assert_not_called()
                self.assertEqual(extractor.archive_bytes, 100)
                self.assertEqual(extractor.bytes, 0)
                with mock.patch.object(cache, "MAX_SELECTED", 2):
                    extractor.add("build/src/out/Default/obj/file.o", "file", 3, 0o644, MTIME, io.BytesIO(b"obj"))
                self.assertEqual(extractor.bytes, 3)
                self.assertEqual((tree / "build/src/out/Default/obj/file.o").read_bytes(), b"obj")
                with mock.patch.object(cache, "MAX_EXTRACTED", 103), self.assertRaisesRegex(
                        cache.CacheMiss, "archive_too_large"):
                    extractor.add("build/src/out/Default/obj/large.o", "file", 1, 0o644, MTIME, stream)
                self.assertEqual(cache.MAX_EXTRACTED, 300 * 1024**3)
                self.assertEqual(cache.DISK_HEADROOM, 4 * 1024**3)
                self.disk_usage.return_value = mock.Mock(free=cache.DISK_HEADROOM + 2)
                with self.assertRaisesRegex(cache.CacheMiss, "insufficient_disk_space"):
                    extractor.add("build/src/base/header.h", "file", 3, 0o644, MTIME, io.BytesIO(b"abc"))
                self.assertEqual((tree / "build/src/base/header.h").stat().st_size, 0)

    def test_low_space_preflight_and_mid_extraction_fall_back(self):
        client = self.fixture_client()
        size = self.manifest["sources"]["windows"]["artifacts"]["x64"]["size_in_bytes"]
        self.disk_usage.return_value = mock.Mock(free=2 * size + cache.DISK_HEADROOM - 1)
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "insufficient_disk_space")
        client.open.assert_not_called()
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})
        self.disk_usage.return_value = mock.Mock(free=1024**4)
        client = self.fixture_client(extra=[("src/tools/clang/data", "file", b"a" * 20)])
        written = []
        original = cache.require_space

        def check_space(path, additional=0):
            if Path(path) == self.destination / "tree" and additional:
                written.append(additional)
                if len(written) == 2:
                    self.disk_usage.return_value = mock.Mock(free=cache.DISK_HEADROOM + additional - 1)
            original(path, additional)

        with mock.patch.object(cache, "require_space", side_effect=check_space), mock.patch.object(cache, "CHUNK", 4):
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "insufficient_disk_space")
        self.assertEqual(len(written), 2)
        self.assertEqual(result["extraction_scope"], "source-and-objects")
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_windows_legacy_scope_hits_are_not_reused(self):
        for old_scope in (None, "toolchains-and-args"):
            with self.subTest(old_scope=old_scope):
                destination = self.root / f"stale-windows-{old_scope}"
                client = self.fixture_client()
                result = cache.fetch("windows", "x64", destination, root=self.root, client=client)
                self.assertEqual(result["status"], "hit", result)
                source = Path(result["source"])
                shutil.rmtree(source / "out/Default/obj")
                (source / "chrome/browser/file.cc").unlink()
                if old_scope is None:
                    result.pop("extraction_scope")
                else:
                    result["extraction_scope"] = old_scope
                (destination / "result.json").write_text(json.dumps(result))
                stale = source / "stale"
                stale.write_text("must not survive")
                client = self.fixture_client()
                self.assertEqual(cache.load_manifest("windows", "x64", root=self.root)[1], result["manifest"])
                result = cache.fetch("windows", "x64", destination, root=self.root, client=client)
                self.assertEqual(result["status"], "hit", result)
                self.assertEqual(result["extraction_scope"], "source-and-objects")
                self.assertEqual((source / "out/Default/obj/file.o").read_bytes(), b"object")
                self.assertEqual((source / "chrome/browser/file.cc").read_bytes(), b"source")
                self.assertFalse(stale.exists())
                client.open.assert_called_once()

    @unittest.skipUnless(shutil.which("zstd"), "host zstd is unavailable")
    def test_posix_legacy_scope_hits_are_not_reused(self):
        for platform, arch in (("linux", "x64"), ("linux", "arm64"), ("macos", "x64"), ("macos", "arm64")):
            source_name = "build/src" if platform == "linux" else "src"
            for old_scope in (None, "toolchains-and-args"):
                with self.subTest(platform=platform, arch=arch, old_scope=old_scope):
                    destination = self.root / f"stale-{platform}-{arch}-{old_scope}"
                    client = self.fixture_client(platform, arch, source_name)
                    result = cache.fetch(platform, arch, destination, root=self.root, client=client)
                    self.assertEqual(result["status"], "hit", result)
                    source = Path(result["source"])
                    shutil.rmtree(source / "out/Default/obj")
                    (source / "chrome/browser/file.cc").unlink()
                    if old_scope is None:
                        result.pop("extraction_scope")
                    else:
                        result["extraction_scope"] = old_scope
                    (destination / "result.json").write_text(json.dumps(result))
                    stale = source / "stale"
                    stale.write_text("must not survive")
                    client = self.fixture_client(platform, arch, source_name)
                    self.assertEqual(cache.load_manifest(platform, arch, root=self.root)[1], result["manifest"])
                    result = cache.fetch(platform, arch, destination, root=self.root, client=client)
                    self.assertEqual(result["status"], "hit", result)
                    self.assertEqual(result["extraction_scope"], "source-and-objects")
                    self.assertEqual((source / "out/Default/obj/file.o").read_bytes(), b"object")
                    self.assertEqual((source / "chrome/browser/file.cc").read_bytes(), b"source")
                    self.assertFalse(stale.exists())
                    client.open.assert_called_once()

    @unittest.skipUnless(shutil.which("zstd"), "host zstd is unavailable")
    def test_posix_low_space_during_source_extraction_cleans_up(self):
        for platform, arch in (("linux", "x64"), ("linux", "arm64"), ("macos", "x64"), ("macos", "arm64")):
            with self.subTest(platform=platform, arch=arch):
                source = "build/src" if platform == "linux" else "src"
                client = self.fixture_client(platform, arch, source)
                self.disk_usage.return_value = mock.Mock(free=1024**4)
                original = cache.require_space

                def check_space(path, additional=0):
                    if Path(path) == self.destination / "tree" and additional:
                        self.disk_usage.return_value = mock.Mock(free=cache.DISK_HEADROOM + additional - 1)
                    original(path, additional)

                with mock.patch.object(cache, "require_space", side_effect=check_space):
                    result = cache.fetch(platform, arch, self.destination, root=self.root, client=client)
                self.assertEqual(result["reason"], "insufficient_disk_space")
                self.assertEqual(result["extraction_scope"], "source-and-objects")
                self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_missing_zstd_is_miss_and_never_runs_cached_tool(self):
        with mock.patch.object(cache.shutil, "which", return_value=None):
            result = cache.fetch("linux", "x64", self.destination, root=self.root)
        self.assertEqual(result["reason"], "zstd_unavailable")
        with mock.patch.object(cache.shutil, "which", return_value=str(self.destination / "zstd")):
            result = cache.fetch("linux", "x64", self.destination, root=self.root)
        self.assertEqual(result["reason"], "unsafe_decompressor")


if __name__ == "__main__":
    unittest.main()
