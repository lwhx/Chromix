#!/usr/bin/env python3
"""Download digest-pinned snapshot ZIPs and atomically publish verified volumes.

The manifest must come from validate_posix_snapshot.py and include repository and
artifact SHA-256 digests. Only ZIP containers are inspected; tar/zstd contents are
not interpreted. GH_TOKEN is sent only to the original GitHub API request.
A verified report confirms content integrity, not publication. Only success with
publication=published confirms both; post-publication report failure preserves
the complete destination and returns a nonzero exit status.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

API = "https://api.github.com"
CHUNK = 1024 * 1024
TIMEOUT = 60
TOTAL_SECONDS = 3600
ATTEMPTS = 3
MAX_REDIRECTS = 5
MAX_FILES = 8
MAX_VOLUME_BYTES = 9 * 1024**3
MAX_TOTAL_BYTES = 72 * 1024**3
MANIFEST_LIMIT = 2 * 1024 * 1024
REDIRECT_CODES = (301, 302, 303, 307, 308)


class SnapshotError(ValueError):
    """A fail-closed error whose message contains no remote URL or credentials."""


class RetryableError(SnapshotError):
    """A transient network error or incomplete response body."""


def require(condition, reason):
    if not condition:
        raise SnapshotError(reason)


def remaining(deadline):
    seconds = deadline - time.monotonic()
    require(seconds > 0, "total_timeout")
    return seconds


def require_space(path, additional):
    require(shutil.disk_usage(path).free >= additional, "insufficient_disk_space")


def load_manifest(path):
    with Path(path).open("rb") as source:
        raw = source.read(MANIFEST_LIMIT + 1)
    require(len(raw) <= MANIFEST_LIMIT, "manifest_too_large")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SnapshotError("invalid_manifest_json") from None
    require(isinstance(data, dict), "invalid_manifest")
    repository = data.get("repository")
    require(isinstance(repository, str) and re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        and all(part not in (".", "..") for part in repository.split("/")), "invalid_repository")
    head_sha = data.get("head_sha")
    require(isinstance(head_sha, str) and re.fullmatch(r"[0-9a-f]{40}", head_sha), "invalid_head_sha")
    require(type(data.get("run_id")) is int and data["run_id"] > 0, "invalid_run_id")
    items = data.get("artifacts")
    require(isinstance(items, list) and 1 <= len(items) <= MAX_FILES, "invalid_artifact_set")
    artifacts, ids, names = [], set(), set()
    for item in items:
        require(isinstance(item, dict), "invalid_artifact")
        identifier, name, size = item.get("id"), item.get("name"), item.get("size_in_bytes")
        require(type(identifier) is int and identifier > 0 and identifier not in ids,
                "invalid_or_duplicate_artifact_id")
        require(isinstance(name, str) and name and len(name) <= 255 and name not in names
                and not any(ord(char) < 32 or ord(char) == 127 for char in name),
                "invalid_or_duplicate_artifact_name")
        require(type(size) is int and size > 0, "invalid_artifact_size")
        require(item.get("expired") is False, "expired_artifact")
        digest = item.get("digest")
        require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest),
                "missing_or_invalid_artifact_digest")
        ids.add(identifier)
        names.add(name)
        artifacts.append({"id": identifier, "name": name, "size_in_bytes": size,
                          "expired": False, "digest": digest.lower()})
    return {"repository": repository, "head_sha": head_sha, "run_id": data["run_id"],
            "artifacts": artifacts}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        # Preserve raw Location for our validator, without urllib's URL parsing.
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def validate_redirect(url):
    try:
        require(isinstance(url, str) and not any(ord(char) <= 32 or ord(char) >= 127 for char in url)
                and "\\" not in url, "unsafe_redirect")
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        require(parsed.scheme == "https" and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None and not parsed.fragment
                and re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
                and (host.endswith(".blob.core.windows.net") or host.endswith(".githubusercontent.com")),
                "unsafe_redirect")
    except ValueError:
        raise SnapshotError("unsafe_redirect") from None


def header_values(headers, name):
    if hasattr(headers, "get_all"):
        return headers.get_all(name, [])
    value = headers.get(name)
    return [] if value is None else [value]


def content_length(response, expected):
    values = header_values(response.headers, "Content-Length")
    require(len(values) == 1 and isinstance(values[0], str)
            and re.fullmatch(r"[0-9]{1,20}", values[0]), "invalid_content_length")
    length = int(values[0])
    require(length == expected, "content_length_mismatch")
    require(not header_values(response.headers, "Transfer-Encoding"), "unexpected_transfer_encoding")
    encodings = header_values(response.headers, "Content-Encoding")
    require(not encodings or encodings == ["identity"], "unexpected_content_encoding")
    return length


class GitHub:
    def __init__(self, token=None):
        self.token = os.environ.get("GH_TOKEN", "") if token is None else token
        require(isinstance(self.token, str) and self.token
                and all(32 < ord(char) < 127 for char in self.token), "missing_or_invalid_gh_token")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def open_artifact(self, repository, identifier, deadline):
        url = f"{API}/repos/{repository}/actions/artifacts/{identifier}/zip"
        for hop in range(MAX_REDIRECTS + 1):
            headers = {"User-Agent": "chromix-posix-snapshot", "Accept-Encoding": "identity"}
            if hop == 0:
                headers.update({"Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28"})
            request = urllib.request.Request(url, headers=headers, method="GET")
            if hop == 0:
                request.add_unredirected_header("Authorization", "Bearer " + self.token)
            try:
                response = self.opener.open(request, timeout=min(TIMEOUT, remaining(deadline)))
            except urllib.error.HTTPError as error:
                response = error
            except SnapshotError:
                raise
            except ValueError:
                raise SnapshotError("unsafe_redirect") from None
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                raise RetryableError("network_error") from None
            if response.status == 200:
                return response
            try:
                status = response.status
                if status in REDIRECT_CODES:
                    locations = header_values(response.headers, "Location")
                    require(len(locations) == 1 and isinstance(locations[0], str), "invalid_redirect")
                    require(hop < MAX_REDIRECTS, "too_many_redirects")
                    location = locations[0]
                    require(not any(ord(char) <= 32 or ord(char) >= 127 for char in location),
                            "unsafe_redirect")
                    try:
                        url = urllib.parse.urljoin(url, location)
                    except ValueError:
                        raise SnapshotError("unsafe_redirect") from None
                    validate_redirect(url)
                    require(self.token not in urllib.parse.unquote(url), "unsafe_redirect")
                elif status in (408, 429) or 500 <= status <= 599:
                    raise RetryableError(f"http_{status}")
                else:
                    raise SnapshotError(f"http_{status}")
            finally:
                response.close()
        raise SnapshotError("too_many_redirects")

    def download(self, repository, artifact, staging, deadline, record):
        expected = artifact["size_in_bytes"]
        for attempt in range(1, ATTEMPTS + 1):
            remaining(deadline)
            require_space(staging, expected)
            evidence = {"attempt": attempt, "status": "failed", "size_in_bytes": 0}
            record["attempts"].append(evidence)
            path, complete, retry = None, False, False
            try:
                with self.open_artifact(repository, artifact["id"], deadline) as response:
                    evidence["content_length"] = content_length(response, expected)
                    digest = hashlib.sha256()
                    with tempfile.NamedTemporaryFile(mode="wb", prefix=".artifact-", suffix=".zip",
                                                     dir=staging, delete=False) as output:
                        path = Path(output.name)
                        while True:
                            remaining(deadline)
                            try:
                                # HTTPResponse.read1 does not wait to fill a multi-MiB buffer.
                                reader = getattr(response, "read1", response.read)
                                chunk = reader(CHUNK)
                            except (urllib.error.URLError, OSError, http.client.HTTPException):
                                raise RetryableError("network_read_error") from None
                            remaining(deadline)
                            if not chunk:
                                break
                            evidence["size_in_bytes"] += len(chunk)
                            require(evidence["size_in_bytes"] <= expected, "download_size_mismatch")
                            require_space(staging, len(chunk))
                            output.write(chunk)
                            digest.update(chunk)
                    if evidence["size_in_bytes"] < expected:
                        raise RetryableError("download_truncated")
                    require(path.stat().st_size == expected, "download_size_mismatch")
                    evidence["sha256"] = digest.hexdigest()
                    require("sha256:" + evidence["sha256"] == artifact["digest"], "checksum_mismatch")
                    evidence["status"] = "success"
                    complete = True
                    return path
            except RetryableError as error:
                evidence["reason"] = str(error)
                retry = True
            except SnapshotError as error:
                evidence["reason"] = str(error)
                raise
            finally:
                if path is not None and not complete:
                    path.unlink(missing_ok=True)
            if retry:
                if attempt == ATTEMPTS:
                    raise SnapshotError(evidence["reason"])
                time.sleep(min(2**(attempt - 1), remaining(deadline)))
        raise SnapshotError("download_failed")


def volume_name(info):
    name = info.orig_filename
    require(name == info.filename and "\\" not in name and not name.startswith("/"),
            "unsafe_zip_name")
    parts = name.split("/")
    require(1 <= len(parts) <= 2 and all(part not in ("", ".", "..")
            and re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts), "unsafe_zip_name")
    require(re.fullmatch(r"tree\.tar\.zst\.[0-9]{3}", parts[-1]), "unexpected_zip_member")
    mode = stat.S_IFMT(info.external_attr >> 16)
    require(not info.is_dir() and not info.external_attr & 0x10
            and mode in (0, stat.S_IFREG) and not info.flag_bits & 1, "non_regular_zip_member")
    require(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), "unsupported_zip_compression")
    return parts[-1]


def extract_volumes(outer, staging, artifact_id, volumes, deadline):
    remaining(deadline)
    with zipfile.ZipFile(outer) as archive:
        members = archive.infolist()
        require(members and len(members) + len(volumes) <= MAX_FILES, "volume_count_limit")
        selected, names = [], set(volumes)
        total = sum(item["size_in_bytes"] for item in volumes.values())
        for info in members:
            name = volume_name(info)
            require(name not in names, "duplicate_volume")
            names.add(name)
            require(0 < info.file_size <= MAX_VOLUME_BYTES, "volume_size_limit")
            total += info.file_size
            require(total <= MAX_TOTAL_BYTES, "total_size_limit")
            selected.append((info, name))
        # Free space already excludes the retained source ZIP and earlier volumes.
        require_space(staging, sum(info.file_size for info, _ in selected))
        for info, name in selected:
            size, digest = 0, hashlib.sha256()
            with archive.open(info) as source, (staging / name).open("xb") as output:
                while True:
                    remaining(deadline)
                    chunk = source.read(CHUNK)
                    remaining(deadline)
                    if not chunk:
                        break
                    size += len(chunk)
                    require(size <= info.file_size and size <= MAX_VOLUME_BYTES, "volume_size_mismatch")
                    require_space(staging, len(chunk))
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            require(size == info.file_size and (staging / name).stat().st_size == size,
                    "volume_size_mismatch")
            volumes[name] = {"name": name, "artifact_id": artifact_id,
                             "size_in_bytes": size, "sha256": digest.hexdigest()}


def publish(staging, destination):
    require(not os.path.lexists(destination), "destination_exists")
    # POSIX rename alone can silently replace an existing empty directory.
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(staging), -100, os.fsencode(destination), 1)
    elif sys.platform == "darwin" and hasattr(library, "renamex_np"):
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(staging), os.fsencode(destination), 0x4)
    else:
        raise SnapshotError("atomic_noreplace_unavailable")
    rename.restype = ctypes.c_int
    if rename(*args) != 0:
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise SnapshotError("destination_exists")
        raise SnapshotError("atomic_publish_failed")


def write_report(path, result):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".snapshot-report-",
                                         dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(result, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def output_paths(manifest, destination, report):
    destination = Path(os.path.abspath(destination))
    report = Path(os.path.abspath(report))
    require(report != destination and not report.is_relative_to(destination), "report_inside_destination")
    destination = destination.parent.resolve(strict=True) / destination.name
    report = report.parent.resolve(strict=True) / report.name
    require(report != destination and not report.is_relative_to(destination)
            and not report.is_relative_to(destination.resolve()), "report_inside_destination")
    require(report.resolve() != Path(manifest).resolve(), "report_overwrites_manifest")
    require(not report.is_symlink(), "report_is_symlink")
    require(destination.parent.is_dir() and report.parent.is_dir(), "output_parent_not_directory")
    return destination, report


def download_snapshot(manifest, destination, report, *, client=None, timeout_seconds=TOTAL_SECONDS):
    started = time.monotonic()
    destination, report = output_paths(manifest, destination, report)
    result = {"status": "failed", "phase": "arguments", "destination": str(destination),
              "artifacts": [], "volumes": [], "publication": "not_attempted", "timeout_seconds": None}
    staging, volumes = None, {}
    try:
        require(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
                and timeout_seconds > 0, "invalid_timeout")
        deadline = started + timeout_seconds
        require(math.isfinite(deadline), "invalid_timeout")
        result.update(phase="manifest", timeout_seconds=timeout_seconds)
        require(not os.path.lexists(destination), "destination_exists")
        metadata = load_manifest(manifest)
        result.update({key: metadata[key] for key in ("repository", "head_sha", "run_id")})
        client = client if client is not None else GitHub()
        remaining(deadline)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))
        for artifact in metadata["artifacts"]:
            record = {**artifact, "attempts": []}
            result["artifacts"].append(record)
            result["phase"] = "download"
            outer = client.download(metadata["repository"], artifact, staging, deadline, record)
            try:
                result["phase"] = "extract_zip"
                extract_volumes(outer, staging, artifact["id"], volumes, deadline)
            finally:
                outer.unlink(missing_ok=True)
        result["phase"] = "validate_sequence"
        require(sorted(volumes) == [f"tree.tar.zst.{index:03d}" for index in range(1, len(volumes) + 1)],
                "noncontiguous_volumes")
        result["volumes"] = [volumes[name] for name in sorted(volumes)]
        result["total_size_in_bytes"] = sum(item["size_in_bytes"] for item in volumes.values())
        result["phase"] = "publish"
        remaining(deadline)
        result.update(status="verified", publication="unconfirmed",
                      duration_seconds=round(time.monotonic() - started, 3))
        # This durable report confirms verification, never successful publication.
        write_report(report, result)
        remaining(deadline)
        publish(staging, destination)
        staging = None
        result.update(status="success", publication="published", phase="complete",
                      duration_seconds=round(time.monotonic() - started, 3))
        try:
            write_report(report, result)
        except OSError:
            raise SnapshotError("published_report_write_failed") from None
        return result
    except (SnapshotError, OSError, zipfile.BadZipFile, zipfile.LargeZipFile,
            EOFError, UnicodeError, zlib.error, NotImplementedError, KeyboardInterrupt) as error:
        if isinstance(error, KeyboardInterrupt):
            reason = "interrupted"
        elif isinstance(error, SnapshotError):
            reason = str(error)
        elif isinstance(error, (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError,
                                UnicodeError, zlib.error, NotImplementedError)):
            reason = "zip_integrity_error"
        else:
            reason = "local_io_error"
        result.update(status="failed", reason=reason,
                      volumes=[volumes[name] for name in sorted(volumes)],
                      duration_seconds=round(time.monotonic() - started, 3))
        try:
            write_report(report, result)
        except OSError:
            if isinstance(error, KeyboardInterrupt):
                raise error from None
            reason = ("published_report_write_failed" if result["publication"] == "published"
                      else "report_write_failed")
            raise SnapshotError(reason) from None
        if isinstance(error, KeyboardInterrupt):
            raise
        raise SnapshotError(reason) from None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=TOTAL_SECONDS)
    args = parser.parse_args(argv)
    try:
        result = download_snapshot(args.manifest, args.destination, args.report,
                                   timeout_seconds=args.timeout_seconds)
    except (SnapshotError, OSError) as error:
        reason = str(error) if isinstance(error, SnapshotError) else "local_io_error"
        print(f"snapshot download failed: {reason}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("snapshot download failed: interrupted", file=sys.stderr)
        return 130
    print(f"snapshot download succeeded: {len(result['volumes'])} verified volumes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
