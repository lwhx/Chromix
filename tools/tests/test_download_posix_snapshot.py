import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
import urllib.error
import warnings
import zipfile
from email.message import Message
from unittest import mock

from tools import download_posix_snapshot as snapshot


REPO = "owner/Chromix"
SHA = "a" * 40
TOKEN = "fixture-token-secret"
SIGNED = "https://fixture.blob.core.windows.net/artifact.zip?sig=fixture-signed-secret"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def zip_bytes(entries, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(output, "w") as archive:
        warnings.simplefilter("ignore", UserWarning)
        for name, kind, data in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.compress_type = compression
            info.external_attr = {"file": stat.S_IFREG | 0o644, "dir": stat.S_IFDIR | 0o755,
                                  "sym": stat.S_IFLNK | 0o777, "fifo": stat.S_IFIFO | 0o600}[kind] << 16
            archive.writestr(info, data)
    return output.getvalue()


class Response(io.BytesIO):
    def __init__(self, data=b"", *, status=200, headers=None, short_read=None, fail_after=None):
        super().__init__(data)
        self.status = status
        self.headers = Message()
        for key, value in (headers if headers is not None else {"Content-Length": str(len(data))}).items():
            self.headers[key] = value
        self.short_read = short_read
        self.fail_after = fail_after
        self.read_sizes = []

    def read1(self, size=-1):
        self.read_sizes.append(size)
        if self.fail_after is not None and self.tell() >= self.fail_after:
            raise http.client.IncompleteRead(b"partial", 100)
        return super().read(size if self.short_read is None else min(size, self.short_read))


class HTTPSFixture(snapshot.urllib.request.HTTPSHandler):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)
        self.requests = []

    def https_open(self, request):
        self.requests.append(request)
        response = next(self.responses)
        response.code = response.status
        response.msg = "fixture"
        response.info = lambda: response.headers
        response.url = request.full_url
        return response


class DownloadPosixSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "snapshot"
        self.report_path = self.root / "report.json"
        self.manifest_path = self.root / "manifest.json"
        self.data = zip_bytes([("tree.tar.zst.001", "file", b"volume-one")])
        self.manifest = {"repository": REPO, "head_sha": SHA, "run_id": 34572987341,
                         "artifacts": []}
        self.set_artifacts([self.data])
        self.client = snapshot.GitHub(TOKEN)
        self.requests = []
        self.responses = []
        self.sleep_patch = mock.patch.object(snapshot.time, "sleep")
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.disk_patch = mock.patch.object(snapshot.shutil, "disk_usage", return_value=mock.Mock(free=1024**4))
        self.disk = self.disk_patch.start()
        self.addCleanup(self.disk_patch.stop)
        self.queue(Response(self.data))

    def set_artifacts(self, data):
        self.manifest["artifacts"] = [
            {"id": 101 + index, "name": f"snapshot-part{index + 1}", "size_in_bytes": len(body),
             "expired": False, "digest": digest(body)} for index, body in enumerate(data)]
        self.save_manifest()

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest))

    def queue(self, *events, on_request=None):
        events = iter(events)

        def open_request(request, timeout):
            self.requests.append(request)
            self.assertEqual(request.get_method(), "GET")
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, snapshot.TIMEOUT)
            if on_request is not None:
                on_request(request)
            event = next(events)
            if isinstance(event, BaseException):
                raise event
            self.responses.append(event)
            return event

        self.client.opener.open = mock.Mock(side_effect=open_request)

    def download(self, **kwargs):
        return snapshot.download_snapshot(self.manifest_path, self.destination, self.report_path,
                                          client=self.client, **kwargs)

    def report(self):
        return json.loads(self.report_path.read_text())

    def assert_clean_failure(self, reason):
        with self.assertRaisesRegex(snapshot.SnapshotError, reason):
            self.download()
        self.assertFalse(os.path.lexists(self.destination))
        self.assertEqual(list(self.root.glob(".snapshot.staging-*")), [])
        self.assertEqual(list(self.root.glob(".snapshot-report-*")), [])
        self.assertEqual(self.report()["status"], "failed")
        self.assertEqual(self.report()["reason"], reason)
        self.assertNotIn(TOKEN, self.report_path.read_text())
        self.assertNotIn("fixture-signed-secret", self.report_path.read_text())

    def test_multi_artifact_success_publishes_only_verified_flat_volumes(self):
        first = zip_bytes([("part/tree.tar.zst.002", "file", b"two")], zipfile.ZIP_DEFLATED)
        second = zip_bytes([("tree.tar.zst.001", "file", b"one"),
                            ("other/tree.tar.zst.003", "file", b"three")])
        self.set_artifacts([first, second])

        def hidden(request):
            self.assertFalse(self.destination.exists())
            if len(self.requests) == 2:
                stages = list(self.root.glob(".snapshot.staging-*"))
                self.assertEqual(len(stages), 1)
                self.assertEqual((stages[0] / "tree.tar.zst.002").read_bytes(), b"two")
                self.assertEqual(list(stages[0].glob("*.zip")), [])

        self.queue(Response(first, short_read=5), Response(second, short_read=7), on_request=hidden)
        original_publish = snapshot.publish

        def publish(staging, destination):
            self.assertEqual(staging.parent, destination.parent)
            self.assertFalse(destination.exists())
            self.assertEqual(self.report()["status"], "verified")
            self.assertEqual(self.report()["publication"], "unconfirmed")
            self.assertEqual(len(self.report()["volumes"]), 3)
            original_publish(staging, destination)

        with mock.patch.object(snapshot, "publish", side_effect=publish) as commit:
            result = self.download()
        self.assertEqual(result, self.report())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["publication"], "published")
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(result["repository"], REPO)
        self.assertEqual(result["head_sha"], SHA)
        self.assertEqual(result["run_id"], 34572987341)
        self.assertEqual(result["total_size_in_bytes"], 11)
        self.assertEqual(result["volumes"], [
            {"name": f"tree.tar.zst.{index:03d}", "artifact_id": artifact_id,
             "size_in_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            for index, artifact_id, body in ((1, 102, b"one"), (2, 101, b"two"), (3, 102, b"three"))])
        self.assertEqual({path.name for path in self.destination.iterdir()},
                         {f"tree.tar.zst.{index:03d}" for index in (1, 2, 3)})
        self.assertEqual((self.destination / "tree.tar.zst.001").read_bytes(), b"one")
        self.assertEqual(list(self.root.glob(".snapshot.staging-*")), [])
        commit.assert_called_once()
        for index, request in enumerate(self.requests):
            self.assertEqual(request.full_url, f"{snapshot.API}/repos/{REPO}/actions/artifacts/{101 + index}/zip")
            self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertTrue(all(response.closed for response in self.responses))
        self.assertTrue(all(size == snapshot.CHUNK for response in self.responses for size in response.read_sizes))

    def test_short_reads_continue_until_eof(self):
        self.queue(Response(self.data, short_read=1))
        result = self.download()
        evidence = result["artifacts"][0]["attempts"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["size_in_bytes"], len(self.data))
        self.assertEqual(evidence[0]["sha256"], digest(self.data).split(":")[1])
        self.assertEqual(self.client.opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_truncated_read_retries_and_deletes_each_incomplete_zip(self):
        headers = {"Content-Length": str(len(self.data))}
        self.queue(Response(self.data[:20], headers=headers), Response(self.data[:30], headers=headers),
                   Response(self.data), on_request=lambda request: self.assertEqual(
                       list(self.root.glob(".snapshot.staging-*/*.zip")), []))
        result = self.download()
        attempts = result["artifacts"][0]["attempts"]
        self.assertEqual([item["status"] for item in attempts], ["failed", "failed", "success"])
        self.assertEqual([item["size_in_bytes"] for item in attempts], [20, 30, len(self.data)])
        self.assertEqual([item["reason"] for item in attempts[:2]], ["download_truncated"] * 2)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_three_truncations_fail_and_clean_staging(self):
        headers = {"Content-Length": str(len(self.data))}
        self.queue(*(Response(self.data[:10], headers=headers) for _ in range(3)))
        self.assert_clean_failure("download_truncated")
        self.assertEqual(self.client.opener.open.call_count, 3)
        self.assertEqual(len(self.report()["artifacts"][0]["attempts"]), 3)

    def test_network_read_error_cleans_partial_before_retry(self):
        self.queue(Response(self.data, short_read=10, fail_after=10), Response(self.data),
                   on_request=lambda request: self.assertEqual(
                       list(self.root.glob(".snapshot.staging-*/*.zip")), []))
        result = self.download()
        self.assertEqual(result["artifacts"][0]["attempts"][0]["reason"], "network_read_error")
        self.assertEqual(result["artifacts"][0]["attempts"][0]["size_in_bytes"], 10)
        self.assertEqual(self.client.opener.open.call_count, 2)

    def test_network_open_error_is_bounded_and_redacted(self):
        self.queue(*(urllib.error.URLError(SIGNED + TOKEN) for _ in range(3)))
        self.assert_clean_failure("network_error")
        self.assertEqual(self.client.opener.open.call_count, 3)

    def test_complete_sha_mismatch_fails_closed_without_opening_zip(self):
        corrupt = bytes([self.data[0] ^ 1]) + self.data[1:]
        self.queue(Response(corrupt), Response(self.data))
        with mock.patch.object(snapshot.zipfile, "ZipFile") as archive:
            self.assert_clean_failure("checksum_mismatch")
        archive.assert_not_called()
        self.assertEqual(self.client.opener.open.call_count, 1)
        attempt = self.report()["artifacts"][0]["attempts"][0]
        self.assertEqual(attempt["sha256"], digest(corrupt).split(":")[1])
        self.sleep.assert_not_called()

    def test_content_length_and_api_size_must_agree(self):
        for headers, reason in (({}, "invalid_content_length"),
                                ({"Content-Length": "abc"}, "invalid_content_length"),
                                ({"Content-Length": "-1"}, "invalid_content_length"),
                                ({"Content-Length": " 123"}, "invalid_content_length"),
                                ({"Content-Length": str(len(self.data) - 1)}, "content_length_mismatch"),
                                ({"Content-Length": str(len(self.data) + 1)}, "content_length_mismatch"),
                                ({"Content-Length": str(len(self.data)), "Content-Encoding": "gzip"},
                                 "unexpected_content_encoding"),
                                ({"Content-Length": str(len(self.data)), "Transfer-Encoding": "chunked"},
                                 "unexpected_transfer_encoding")):
            with self.subTest(headers=headers):
                self.queue(Response(self.data, headers=headers))
                self.assert_clean_failure(reason)
                self.assertEqual(self.client.opener.open.call_count, 1)

    def test_duplicate_content_length_rejected(self):
        response = Response(self.data)
        response.headers["Content-Length"] = str(len(self.data))
        self.queue(response)
        self.assert_clean_failure("invalid_content_length")

    def test_body_larger_than_pinned_size_fails_without_retry(self):
        self.queue(Response(self.data + b"extra", headers={"Content-Length": str(len(self.data))}))
        self.assert_clean_failure("download_size_mismatch")
        self.assertEqual(self.client.opener.open.call_count, 1)

    def test_crc_mismatch_fails_before_publish_even_with_matching_zip_digest(self):
        corrupted = bytearray(self.data)
        name_length, extra_length = struct.unpack_from("<HH", corrupted, 26)
        corrupted[30 + name_length + extra_length] ^= 1
        corrupted = bytes(corrupted)
        self.set_artifacts([corrupted])
        self.queue(Response(corrupted))
        with mock.patch.object(snapshot, "publish") as publish:
            self.assert_clean_failure("zip_integrity_error")
        publish.assert_not_called()
        self.assertEqual(self.report()["phase"], "extract_zip")
        self.assertEqual(self.report()["artifacts"][0]["attempts"][0]["status"], "success")

    def test_bad_zip_structure_fails_before_publish(self):
        self.set_artifacts([b"not-a-zip"])
        self.queue(Response(b"not-a-zip"))
        self.assert_clean_failure("zip_integrity_error")

    def test_redirect_and_token_isolation(self):
        second = "https://results.actions.githubusercontent.com/artifact.zip?sig=other-secret"
        redirected = urllib.error.HTTPError(snapshot.API, 302, "redirect", {"Location": SIGNED}, None)
        self.queue(redirected, Response(status=307, headers={"Location": second}), Response(self.data))
        self.download()
        self.assertEqual([request.full_url for request in self.requests[1:]], [SIGNED, second])
        self.assertEqual(self.requests[0].get_header("Authorization"), "Bearer " + TOKEN)
        self.assertNotIn("Authorization", self.requests[0].headers)
        for request in self.requests[1:]:
            self.assertIsNone(request.get_header("Authorization"))
            self.assertIsNone(request.get_header("Accept"))
            self.assertIsNone(request.get_header("X-github-api-version"))
        self.assertIsNone(snapshot.NoRedirect().redirect_request(None, None, 302, "", {}, SIGNED))
        self.assertTrue(all(response.closed for response in self.responses))
        self.assertNotIn("sig=", self.report_path.read_text())
        self.assertNotIn(TOKEN, self.report_path.read_text())
        self.assertTrue(any(isinstance(handler, snapshot.NoRedirect) for handler in self.client.opener.handlers))

    def test_redirect_retry_restarts_original_api_not_stale_signed_url(self):
        headers = {"Content-Length": str(len(self.data))}
        self.queue(Response(status=302, headers={"Location": SIGNED}), Response(self.data[:5], headers=headers),
                   Response(status=302, headers={"Location": SIGNED + "-new"}), Response(self.data))
        self.download()
        self.assertEqual(self.requests[0].full_url, self.requests[2].full_url)
        for index, request in enumerate(self.requests):
            self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN if index % 2 == 0 else None)

    def test_redirect_allowlist_rejects_http_credentials_api_and_host_tricks(self):
        urls = ["http://fixture.blob.core.windows.net/a", "https://evil.example/a",
                "https://fixture.blob.core.windows.net.evil.example/a", "https://githubusercontent.com/a",
                "https://fixture.githubusercontent.com.evil.example/a", "https://api.github.com/other",
                "https://user:pass@fixture.blob.core.windows.net/a", "https://fixture.blob.core.windows.net:444/a",
                "https://fixture.blob.core.windows.net/a#fragment", "https://127.0.0.1/a", "file:///tmp/a",
                "https://fixture.blob.core.windows.net\\@evil.example/a", "\n" + SIGNED,
                "https://fixture.blob.core.windows.net/a\r\nInjected:yes", "https://[malformed/a",
                "https://fixture.blob.core.windows.net:invalid/a", SIGNED + "&token=" + TOKEN]
        for url in urls:
            with self.subTest(url=url):
                self.queue(Response(status=302, headers={"Location": url}))
                self.assert_clean_failure("unsafe_redirect")
                self.assertEqual(self.client.opener.open.call_count, 1)

    def test_real_redirect_handler_rejects_malformed_ipv6_and_raw_locations(self):
        for status in snapshot.REDIRECT_CODES:
            for location in ("https://[malformed/a?sig=" + TOKEN, "\n" + SIGNED, "file:///tmp/fixture"):
                with self.subTest(status=status, location=location):
                    response = Response(status=status, headers={"Location": location})
                    handler = HTTPSFixture([response])
                    self.client.opener = snapshot.urllib.request.build_opener(
                        snapshot.urllib.request.ProxyHandler({}), snapshot.NoRedirect(), handler)
                    self.assert_clean_failure("unsafe_redirect")
                    self.assertEqual(len(handler.requests), 1)
                    self.assertTrue(response.closed)
                    self.assertEqual(self.report()["phase"], "download")
        self.sleep.assert_not_called()

    def test_real_redirect_handler_preserves_manual_token_isolation(self):
        redirected = Response(status=302, headers={"Location": SIGNED})
        body = Response(self.data)
        handler = HTTPSFixture([redirected, body])
        self.client.opener = snapshot.urllib.request.build_opener(
            snapshot.urllib.request.ProxyHandler({}), snapshot.NoRedirect(), handler)
        self.download()
        self.assertEqual(len(handler.requests), 2)
        self.assertEqual(handler.requests[0].get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(handler.requests[1].full_url, SIGNED)
        self.assertIsNone(handler.requests[1].get_header("Authorization"))
        self.assertTrue(redirected.closed)
        self.assertTrue(body.closed)

    def test_opener_value_error_is_safe_and_reported_without_retry(self):
        self.queue(ValueError(SIGNED + TOKEN))
        self.assert_clean_failure("unsafe_redirect")
        self.assertEqual(self.client.opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_relative_redirect_is_allowed_only_on_trusted_blob_host(self):
        self.queue(Response(status=302, headers={"Location": SIGNED}),
                   Response(status=302, headers={"Location": "/new.zip?sig=relative-secret"}), Response(self.data))
        self.download()
        self.assertEqual(self.requests[2].full_url,
                         "https://fixture.blob.core.windows.net/new.zip?sig=relative-secret")
        self.assertIsNone(self.requests[2].get_header("Authorization"))

    def test_redirect_count_is_bounded(self):
        self.queue(*(Response(status=302, headers={"Location": SIGNED}) for _ in range(6)))
        self.assert_clean_failure("too_many_redirects")
        self.assertEqual(self.client.opener.open.call_count, 6)

    def test_http_failures_only_retry_transient_statuses(self):
        for status in (401, 403, 404, 410, 408, 429, 500, 502, 503):
            with self.subTest(status=status):
                count = 3 if status in (408, 429, 500, 502, 503) else 1
                self.queue(*(urllib.error.HTTPError(SIGNED, status, TOKEN, {}, None) for _ in range(count)))
                self.assert_clean_failure(f"http_{status}")
                self.assertEqual(self.client.opener.open.call_count, count)

    def test_duplicate_basename_rejected_within_or_across_artifacts(self):
        fixtures = [
            [zip_bytes([("tree.tar.zst.001", "file", b"a"), ("tree.tar.zst.001", "file", b"b")])],
            [zip_bytes([("a/tree.tar.zst.001", "file", b"a"), ("b/tree.tar.zst.001", "file", b"b")])],
            [self.data, zip_bytes([("part/tree.tar.zst.001", "file", b"b")])],
        ]
        for artifacts in fixtures:
            with self.subTest(artifacts=len(artifacts)):
                self.set_artifacts(artifacts)
                self.queue(*(Response(body) for body in artifacts))
                self.assert_clean_failure("duplicate_volume")

    def test_missing_first_middle_or_out_of_range_volume_rejected(self):
        for indices in ((2,), (1, 3), (0, 1), (1, 999)):
            with self.subTest(indices=indices):
                data = zip_bytes([(f"tree.tar.zst.{index:03d}", "file", b"volume") for index in indices])
                self.set_artifacts([data])
                self.queue(Response(data))
                self.assert_clean_failure("noncontiguous_volumes")

    def test_unsafe_member_names_and_unnecessary_directories_rejected(self):
        names = ["/tree.tar.zst.001", "../tree.tar.zst.001", "a/../tree.tar.zst.001",
                 r"a\tree.tar.zst.001", r"..\tree.tar.zst.001", "C:/tree.tar.zst.001",
                 "./tree.tar.zst.001", "a//tree.tar.zst.001", "a/b/tree.tar.zst.001",
                 "//host/tree.tar.zst.001", "a:/tree.tar.zst.001", "a/./tree.tar.zst.001",
                 "tree.tar.zst.001/", "part/", "tree.tar.zst.01", "tree.tar.zst.001.exe", "readme.txt"]
        for name in names:
            with self.subTest(name=name):
                data = zip_bytes([(name, "file", b"volume")])
                self.set_artifacts([data])
                self.queue(Response(data))
                reason = "unsafe_zip_name" if name in names[:14] else "unexpected_zip_member"
                self.assert_clean_failure(reason)
        self.assertFalse((self.root / "tree.tar.zst.001").exists())

    def test_nul_in_zip_member_is_rejected_before_normalization(self):
        original = "tree.tar.zst.001x"
        data = zip_bytes([(original, "file", b"volume")]).replace(original.encode(), b"tree.tar.zst.001\x00")
        self.set_artifacts([data])
        self.queue(Response(data))
        self.assert_clean_failure("unsafe_zip_name")

    def test_symlinks_directories_and_special_members_are_rejected(self):
        for kind in ("sym", "dir", "fifo"):
            with self.subTest(kind=kind):
                data = zip_bytes([("tree.tar.zst.001", kind, b"volume")])
                self.set_artifacts([data])
                self.queue(Response(data))
                self.assert_clean_failure("non_regular_zip_member")

    def test_windows_zip_without_unix_file_type_is_accepted(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            info = zipfile.ZipInfo("tree.tar.zst.001")
            info.create_system = 0
            info.external_attr = 0x20
            archive.writestr(info, b"volume")
        data = output.getvalue()
        self.set_artifacts([data])
        self.queue(Response(data))
        self.download()
        self.assertTrue((self.destination / "tree.tar.zst.001").is_file())

    def test_zip64_member_header_is_supported(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            with archive.open("tree.tar.zst.001", "w", force_zip64=True) as member:
                member.write(b"volume")
        data = output.getvalue()
        self.set_artifacts([data])
        self.queue(Response(data))
        self.download()
        self.assertEqual((self.destination / "tree.tar.zst.001").read_bytes(), b"volume")

    def test_final_crc_mismatch_after_partial_volume_write_never_publishes(self):
        data = zip_bytes([("tree.tar.zst.001", "file", b"0123456789" * 2000)])
        corrupted = bytearray(data)
        name_length, extra_length = struct.unpack_from("<HH", corrupted, 26)
        corrupted[30 + name_length + extra_length + 19999] ^= 1
        corrupted = bytes(corrupted)
        self.set_artifacts([corrupted])
        self.queue(Response(corrupted))
        with mock.patch.object(snapshot, "CHUNK", 1024):
            self.assert_clean_failure("zip_integrity_error")

    def test_encrypted_zip_member_is_rejected(self):
        data = bytearray(self.data)
        central = data.index(b"PK\x01\x02")
        for offset in (6, central + 8):
            flags = struct.unpack_from("<H", data, offset)[0]
            struct.pack_into("<H", data, offset, flags | 1)
        data = bytes(data)
        self.set_artifacts([data])
        self.queue(Response(data))
        self.assert_clean_failure("non_regular_zip_member")

    def test_eight_volumes_allowed_but_nine_or_empty_archives_rejected(self):
        data = zip_bytes([(f"tree.tar.zst.{index:03d}", "file", b"v") for index in range(1, 9)])
        self.set_artifacts([data])
        self.queue(Response(data))
        result = self.download()
        self.assertEqual(len(result["volumes"]), 8)
        self.destination = self.root / "too-many"
        for artifacts in ([data, zip_bytes([("tree.tar.zst.009", "file", b"v")])], [zip_bytes([])]):
            with self.subTest(artifacts=len(artifacts)):
                self.set_artifacts(artifacts)
                self.queue(*(Response(body) for body in artifacts))
                self.assert_clean_failure("volume_count_limit")
                self.assertEqual(list(self.root.glob(".too-many.staging-*")), [])

    def test_volume_and_total_size_limits(self):
        self.assertEqual(snapshot.MAX_VOLUME_BYTES, 9 * 1024**3)
        self.assertEqual(snapshot.MAX_TOTAL_BYTES, 72 * 1024**3)
        with mock.patch.object(snapshot, "MAX_VOLUME_BYTES", 3):
            self.assert_clean_failure("volume_size_limit")
        second = zip_bytes([("tree.tar.zst.002", "file", b"two")])
        self.set_artifacts([self.data, second])
        self.queue(Response(self.data), Response(second))
        with mock.patch.object(snapshot, "MAX_TOTAL_BYTES", len(b"volume-one") + 2):
            self.assert_clean_failure("total_size_limit")

    def test_empty_volume_rejected(self):
        data = zip_bytes([("tree.tar.zst.001", "file", b"")])
        self.set_artifacts([data])
        self.queue(Response(data))
        self.assert_clean_failure("volume_size_limit")

    def test_source_zip_space_checked_before_network(self):
        self.disk.return_value.free = len(self.data) - 1
        self.assert_clean_failure("insufficient_disk_space")
        self.client.opener.open.assert_not_called()

    def test_extraction_space_accounts_for_still_present_source_zip(self):
        original = snapshot.require_space
        observed = []

        def check_space(path, additional):
            files = list(Path(path).glob("*.zip"))
            if files and files[0].stat().st_size == len(self.data) and additional == len(b"volume-one"):
                observed.append(files[0])
                raise snapshot.SnapshotError("insufficient_disk_space")
            original(path, additional)

        with mock.patch.object(snapshot, "require_space", side_effect=check_space):
            self.assert_clean_failure("insufficient_disk_space")
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.report()["phase"], "extract_zip")

    def test_existing_destination_file_directory_and_dangling_link_are_untouched(self):
        for kind in ("directory", "empty-directory", "file", "link"):
            with self.subTest(kind=kind):
                self.destination = self.root / kind
                if kind in ("directory", "empty-directory"):
                    self.destination.mkdir()
                    if kind == "directory":
                        (self.destination / "sentinel").write_bytes(b"important")
                elif kind == "file":
                    self.destination.write_bytes(b"important")
                else:
                    self.destination.symlink_to(self.root / "missing")
                before = self.destination.lstat()
                with self.assertRaisesRegex(snapshot.SnapshotError, "destination_exists"):
                    self.download()
                self.assertEqual(self.destination.lstat(), before)
                if kind == "directory":
                    self.assertEqual((self.destination / "sentinel").read_bytes(), b"important")
                elif kind == "file":
                    self.assertEqual(self.destination.read_bytes(), b"important")
                elif kind == "link":
                    self.assertTrue(self.destination.is_symlink())
        self.client.opener.open.assert_not_called()

    def test_destination_created_during_download_is_preserved(self):
        def create_destination(request):
            self.destination.mkdir()
            (self.destination / "sentinel").write_text("other-owner")
        self.queue(Response(self.data), on_request=create_destination)
        with self.assertRaisesRegex(snapshot.SnapshotError, "destination_exists"):
            self.download()
        self.assertEqual((self.destination / "sentinel").read_text(), "other-owner")
        self.assertEqual(list(self.root.glob(".snapshot.staging-*")), [])
        self.assertEqual(self.report()["status"], "failed")

    def test_atomic_publish_does_not_replace_empty_directory_in_race(self):
        staging = self.root / "staging"
        staging.mkdir()
        (staging / "volume").write_text("owned")
        self.destination.mkdir()
        before = self.destination.stat().st_ino
        with mock.patch.object(snapshot.os.path, "lexists", return_value=False), \
                self.assertRaisesRegex(snapshot.SnapshotError, "destination_exists"):
            snapshot.publish(staging, self.destination)
        self.assertEqual(self.destination.stat().st_ino, before)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual((staging / "volume").read_text(), "owned")

    def test_macos_publish_uses_exclusive_rename_flag(self):
        library = mock.Mock()
        library.renamex_np.return_value = 0
        staging = self.root / "staging"
        with mock.patch.object(snapshot.sys, "platform", "darwin"), \
                mock.patch.object(snapshot.ctypes, "CDLL", return_value=library):
            snapshot.publish(staging, self.destination)
        library.renamex_np.assert_called_once_with(os.fsencode(staging), os.fsencode(self.destination), 0x4)

    def test_missing_or_invalid_digest_fails_before_any_request(self):
        for value in (None, "", "sha256:" + "z" * 64, "sha1:" + "a" * 40, "sha256:" + "a" * 63):
            with self.subTest(value=value):
                self.manifest["artifacts"][0]["digest"] = value
                self.save_manifest()
                self.assert_clean_failure("missing_or_invalid_artifact_digest")
        del self.manifest["artifacts"][0]["digest"]
        self.save_manifest()
        self.assert_clean_failure("missing_or_invalid_artifact_digest")
        self.client.opener.open.assert_not_called()

    def test_invalid_manifest_identity_and_artifact_fields_rejected_before_network(self):
        saved = copy.deepcopy(self.manifest)
        for field, value, reason in (("repository", "https://evil.example/repo", "invalid_repository"),
                                     ("repository", "../repo", "invalid_repository"),
                                     ("head_sha", "bad", "invalid_head_sha"),
                                     ("run_id", True, "invalid_run_id"),
                                     ("artifacts", [], "invalid_artifact_set")):
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(saved)
                self.manifest[field] = value
                self.save_manifest()
                self.assert_clean_failure(reason)
        for field, value, reason in (("expired", True, "expired_artifact"),
                                     ("id", True, "invalid_or_duplicate_artifact_id"),
                                     ("size_in_bytes", False, "invalid_artifact_size"),
                                     ("size_in_bytes", 0, "invalid_artifact_size"),
                                     ("name", "", "invalid_or_duplicate_artifact_name")):
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(saved)
                self.manifest["artifacts"][0][field] = value
                self.save_manifest()
                self.assert_clean_failure(reason)
        self.client.opener.open.assert_not_called()

    def test_failed_second_artifact_never_exposes_first_volume(self):
        second = zip_bytes([("tree.tar.zst.002", "file", b"two")])
        self.set_artifacts([self.data, second])
        self.manifest["artifacts"][1]["digest"] = digest(b"wrong")
        self.save_manifest()
        self.queue(Response(self.data), Response(second))
        with mock.patch.object(snapshot, "publish") as publish:
            self.assert_clean_failure("checksum_mismatch")
        publish.assert_not_called()
        self.assertEqual([item["name"] for item in self.report()["volumes"]], ["tree.tar.zst.001"])

    def test_report_failure_prevents_publish_and_cleans_only_owned_staging(self):
        other = self.root / ".snapshot.staging-other-owner"
        other.mkdir()
        sentinel = other / "sentinel"
        sentinel.write_text("important")
        with mock.patch.object(snapshot, "write_report", side_effect=OSError(SIGNED + TOKEN)), \
                mock.patch.object(snapshot, "publish") as publish, \
                self.assertRaisesRegex(snapshot.SnapshotError, "report_write_failed"):
            self.download()
        publish.assert_not_called()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".snapshot.staging-*")), [other])
        self.assertEqual(sentinel.read_text(), "important")

    def test_publish_failure_rewrites_verified_report_to_failed(self):
        with mock.patch.object(snapshot, "publish", side_effect=snapshot.SnapshotError("atomic_publish_failed")):
            self.assert_clean_failure("atomic_publish_failed")
        self.assertEqual(self.report()["phase"], "publish")

    def test_publish_and_failure_report_errors_never_leave_success(self):
        original = snapshot.write_report
        statuses = []

        def write_report(path, result):
            statuses.append(result["status"])
            if result["status"] == "failed":
                raise OSError("report unavailable")
            original(path, result)

        with mock.patch.object(snapshot, "write_report", side_effect=write_report), \
                mock.patch.object(snapshot, "publish", side_effect=snapshot.SnapshotError("atomic_publish_failed")), \
                self.assertRaisesRegex(snapshot.SnapshotError, "report_write_failed"):
            self.download()
        self.assertEqual(statuses, ["verified", "failed"])
        self.assertEqual(self.report()["status"], "verified")
        self.assertEqual(self.report()["publication"], "unconfirmed")
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".snapshot.staging-*")), [])

    def test_final_report_error_preserves_published_tree_and_verified_report(self):
        original = snapshot.write_report
        statuses = []

        def write_report(path, result):
            statuses.append(result["status"])
            if result["status"] != "verified":
                self.assertTrue(self.destination.is_dir())
                raise OSError("report unavailable")
            self.assertFalse(self.destination.exists())
            original(path, result)

        with mock.patch.object(snapshot, "write_report", side_effect=write_report), \
                self.assertRaisesRegex(snapshot.SnapshotError, "published_report_write_failed"):
            self.download()
        self.assertEqual(statuses, ["verified", "success", "failed"])
        self.assertEqual(self.report()["status"], "verified")
        self.assertEqual(self.report()["publication"], "unconfirmed")
        self.assertEqual((self.destination / "tree.tar.zst.001").read_bytes(), b"volume-one")
        self.assertEqual(list(self.root.glob(".snapshot.staging-*")), [])

    def test_final_report_error_records_publication_when_failure_report_succeeds(self):
        original = snapshot.write_report

        def write_report(path, result):
            if result["status"] == "success":
                raise OSError("final report unavailable")
            original(path, result)

        with mock.patch.object(snapshot, "write_report", side_effect=write_report), \
                self.assertRaisesRegex(snapshot.SnapshotError, "published_report_write_failed"):
            self.download()
        self.assertEqual(self.report()["status"], "failed")
        self.assertEqual(self.report()["publication"], "published")
        self.assertEqual(self.report()["reason"], "published_report_write_failed")
        self.assertEqual((self.destination / "tree.tar.zst.001").read_bytes(), b"volume-one")

    def test_final_report_error_never_deletes_replacement_destination(self):
        original = snapshot.write_report
        retained = self.root / "published-snapshot"

        def write_report(path, result):
            if result["status"] == "success":
                self.destination.rename(retained)
                self.destination.mkdir()
                (self.destination / "sentinel").write_text("other-owner")
                raise OSError("final report unavailable")
            original(path, result)

        with mock.patch.object(snapshot, "write_report", side_effect=write_report), \
                self.assertRaisesRegex(snapshot.SnapshotError, "published_report_write_failed"):
            self.download()
        self.assertEqual((self.destination / "sentinel").read_text(), "other-owner")
        self.assertEqual((retained / "tree.tar.zst.001").read_bytes(), b"volume-one")

    def test_report_cannot_target_destination_or_overwrite_manifest(self):
        original = self.manifest_path.read_bytes()
        for report, reason in ((self.destination, "report_inside_destination"),
                               (self.manifest_path, "report_overwrites_manifest")):
            self.report_path = report
            with self.subTest(report=report), self.assertRaisesRegex(snapshot.SnapshotError, reason):
                self.download()
        self.assertEqual(self.manifest_path.read_bytes(), original)
        self.assertFalse(self.destination.exists())
        self.client.opener.open.assert_not_called()

    def test_report_inside_existing_symlink_destination_never_mutates_target(self):
        target = self.root / "other-owner"
        target.mkdir()
        sentinel = target / "report.json"
        sentinel.write_text("important")
        self.destination.symlink_to(target, target_is_directory=True)
        for report in (self.destination / "report.json", sentinel):
            self.report_path = report
            with self.subTest(report=report), self.assertRaisesRegex(snapshot.SnapshotError, "report_inside_destination"):
                self.download()
        self.assertEqual(sentinel.read_text(), "important")
        self.assertTrue(self.destination.is_symlink())
        self.client.opener.open.assert_not_called()

    def test_report_inside_new_destination_fails_without_creating_it(self):
        self.report_path = self.destination / "report.json"
        with self.assertRaisesRegex(snapshot.SnapshotError, "report_inside_destination"):
            self.download()
        self.assertFalse(self.destination.exists())
        self.client.opener.open.assert_not_called()

    def test_interruption_removes_partial_zip_and_owned_staging(self):
        response = Response(self.data)
        response.read1 = mock.Mock(side_effect=[self.data[:10], KeyboardInterrupt()])
        self.queue(response)
        with self.assertRaises(KeyboardInterrupt):
            self.download()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".snapshot.staging-*")), [])
        self.assertTrue(response.closed)
        self.assertEqual(self.report()["status"], "failed")
        self.assertEqual(self.report()["reason"], "interrupted")
        self.assertEqual(self.report()["phase"], "download")

    def test_interruption_after_rename_preserves_complete_destination(self):
        original = snapshot.publish

        def publish(staging, destination):
            original(staging, destination)
            raise KeyboardInterrupt

        with mock.patch.object(snapshot, "publish", side_effect=publish), self.assertRaises(KeyboardInterrupt):
            self.download()
        self.assertEqual((self.destination / "tree.tar.zst.001").read_bytes(), b"volume-one")
        self.assertEqual(list(self.root.glob(".snapshot.staging-*")), [])
        self.assertEqual(self.report()["status"], "failed")
        self.assertEqual(self.report()["reason"], "interrupted")
        self.assertEqual(self.report()["publication"], "unconfirmed")

    def test_invalid_timeout_writes_valid_failure_json_before_network(self):
        for value in (0, -1, float("nan"), float("inf"), float("-inf"), True, None, "bad"):
            with self.subTest(timeout=value), self.assertRaisesRegex(snapshot.SnapshotError, "invalid_timeout"):
                self.download(timeout_seconds=value)
            result = self.report()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["phase"], "arguments")
            self.assertEqual(result["reason"], "invalid_timeout")
            self.assertIsNone(result["timeout_seconds"])
            self.assertNotIn("NaN", self.report_path.read_text())
            self.assertNotIn("Infinity", self.report_path.read_text())
            self.assertFalse(self.destination.exists())
        self.client.opener.open.assert_not_called()

    def test_shared_deadline_expires_during_retry_without_another_request(self):
        now = [0.0]
        self.queue(Response(self.data[:1], headers={"Content-Length": str(len(self.data))}))
        self.sleep.side_effect = lambda seconds: now.__setitem__(0, now[0] + seconds)
        with mock.patch.object(snapshot.time, "monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(snapshot.SnapshotError, "total_timeout"):
                self.download(timeout_seconds=1)
        self.assertEqual(self.client.opener.open.call_count, 1)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".snapshot.staging-*")), [])
        self.assertEqual(self.report()["reason"], "total_timeout")

    def test_all_artifacts_share_the_same_deadline(self):
        second = zip_bytes([("tree.tar.zst.002", "file", b"two")])
        self.set_artifacts([self.data, second])
        self.queue(Response(self.data), Response(second))
        now, deadlines = [0.0], []
        original = self.client.download

        def download(repository, artifact, staging, deadline, record):
            deadlines.append(deadline)
            result = original(repository, artifact, staging, deadline, record)
            now[0] += 2
            return result

        with mock.patch.object(snapshot.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(self.client, "download", side_effect=download):
            self.download(timeout_seconds=10)
        self.assertEqual(deadlines, [10, 10])

    def test_deadline_checked_after_read_before_writing(self):
        now = [0.0]
        response = Response(self.data)
        original = response.read1

        def slow_read(size):
            result = original(size)
            now[0] = snapshot.TOTAL_SECONDS + 1
            return result

        response.read1 = slow_read
        self.queue(response)
        with mock.patch.object(snapshot.time, "monotonic", side_effect=lambda: now[0]):
            self.assert_clean_failure("total_timeout")
        self.assertEqual(self.client.opener.open.call_count, 1)
        self.assertEqual(self.report()["artifacts"][0]["attempts"][0]["size_in_bytes"], 0)

    def test_cli_success_uses_manifest_report_and_gh_token(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": TOKEN}, clear=True), \
                mock.patch.object(snapshot.urllib.request, "build_opener", return_value=self.client.opener), \
                mock.patch("sys.stdout", new=io.StringIO()) as stdout, \
                mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            code = snapshot.main(["--manifest", str(self.manifest_path), "--destination", str(self.destination),
                                  "--report", str(self.report_path), "--timeout-seconds", "30"])
        self.assertEqual(code, 0)
        self.assertIn("1 verified volumes", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.requests[0].get_header("Authorization"), "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, stdout.getvalue())
        self.assertEqual(self.report()["timeout_seconds"], 30)

    def test_cli_failure_redacts_signed_url_and_token(self):
        self.queue(Response(status=302, headers={"Location": SIGNED}), urllib.error.URLError(SIGNED + TOKEN),
                   urllib.error.URLError(SIGNED + TOKEN), urllib.error.URLError(SIGNED + TOKEN))
        with mock.patch.dict(os.environ, {"GH_TOKEN": TOKEN}, clear=True), \
                mock.patch.object(snapshot.urllib.request, "build_opener", return_value=self.client.opener), \
                mock.patch("sys.stdout", new=io.StringIO()) as stdout, \
                mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            code = snapshot.main(["--manifest", str(self.manifest_path), "--destination", str(self.destination),
                                  "--report", str(self.report_path)])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("network_error", stderr.getvalue())
        self.assertNotIn(TOKEN, stderr.getvalue())
        self.assertNotIn("sig=", stderr.getvalue())
        self.assertFalse(self.destination.exists())

    def test_cli_interrupt_returns_130_and_writes_failure_report(self):
        self.queue(KeyboardInterrupt())
        with mock.patch.dict(os.environ, {"GH_TOKEN": TOKEN}, clear=True), \
                mock.patch.object(snapshot.urllib.request, "build_opener", return_value=self.client.opener), \
                mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            code = snapshot.main(["--manifest", str(self.manifest_path), "--destination", str(self.destination),
                                  "--report", str(self.report_path)])
        self.assertEqual(code, 130)
        self.assertEqual(stderr.getvalue(), "snapshot download failed: interrupted\n")
        self.assertEqual(self.report()["reason"], "interrupted")
        self.assertFalse(self.destination.exists())

    def test_missing_token_is_rejected_and_reported(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "not-used"}, clear=True), \
                self.assertRaisesRegex(snapshot.SnapshotError, "missing_or_invalid_gh_token"):
            snapshot.download_snapshot(self.manifest_path, self.destination, self.report_path)
        self.assertEqual(self.report()["reason"], "missing_or_invalid_gh_token")
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
