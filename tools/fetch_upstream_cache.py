#!/usr/bin/env python3
"""Fetch pinned upstream snapshots, never build or execute their contents.

GH_TOKEN authenticates GitHub API requests only. Availability/validation failures
write a miss and exit zero; invalid arguments or destination paths exit nonzero.
Linux/macOS require a host zstd executable. The destination must be dedicated to
this tool; result.json records ownership and the absolute source path on a hit.
Linux keeps the full pinned source root, including compiled objects and Ninja state.
Windows/macOS keep only toolchains, update scripts, version markers, and donor args.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "build/upstream-cache.json"
OWNER = "chromix-upstream-cache-v1"
API = "https://api.github.com"
CHUNK = 1024 * 1024
TIMEOUT = 60
DOWNLOAD_SECONDS = 15 * 60
ATTEMPTS = 3
MAX_EXTRACTED = 300 * 1024**3
MAX_SELECTED = 30 * 1024**3
DISK_HEADROOM = 4 * 1024**3
SOURCE_SCOPE = "source-and-objects"
TOOLCHAIN_SCOPE = "toolchains-and-args"
MAX_MEMBERS = 3_000_000
SOURCES = {
    "linux": ("portablelinux", "UngoogledLinuxCommit", ["build/src"]),
    "macos": ("macos", "UngoogledMacOSCommit", ["src"]),
    "windows": ("windows", "UngoogledWindowsCommit", ["src", "build/src"]),
}


class CacheMiss(Exception):
    """An unavailable, untrusted, or incompatible cache; use a source build."""


class LocalError(Exception):
    """An invalid local argument or unsafe destination."""


def require(condition, reason):
    if not condition:
        raise CacheMiss(reason)


def sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            result.update(chunk)
    return "sha256:" + result.hexdigest()


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "invalid_expiry")
        return parsed
    except (AttributeError, TypeError, ValueError) as exc:
        raise CacheMiss("invalid_expiry") from exc


def load_manifest(platform, arch, run_id=None, root=ROOT):
    path = root / "build/upstream-cache.json"
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw)
        pins = dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"\r\n]+)"\s*$',
                               (root / "build/ungoogled-revisions.psd1").read_text(), re.M))
        version = (root / "CHROMIUM_VERSION").read_text().strip()
        require(manifest["schema_version"] == 1, "manifest_schema")
        require(version == pins["ChromiumVersion"] == manifest["chromium_version"]
                and manifest["ungoogled_commit"] == pins["UngoogledCommit"], "pin_mismatch")
        require(set(manifest["sources"]) == set(SOURCES), "manifest_targets")
        for target, (repo, pin_key, roots) in SOURCES.items():
            source = manifest["sources"][target]
            require(source["repository"] == f"ungoogled-software/ungoogled-chromium-{repo}"
                    and source["head_sha"] == pins[pin_key]
                    and re.fullmatch(r"[a-f0-9]{40}", source["head_sha"]), "pin_mismatch")
            require(source["source_roots"] == roots
                    and source["event"] in ("push", "workflow_dispatch")
                    and source["head_branch"] in (version, pins[pin_key.replace("Commit", "Version")])
                    and source["workflow_path"] == (
                        ".github/workflows/build-x64.yml" if target == "windows"
                        else ".github/workflows/build.yml"), "untrusted_manifest_source")
            for key in ("repository_id", "run_id"):
                require(type(source[key]) is int and source[key] > 0, "manifest_id")
            require(set(source["artifacts"]) == ({"x64"} if target == "windows"
                                                else {"x64", "arm64"}), "manifest_targets")
            for artifact in source["artifacts"].values():
                require(type(artifact["id"]) is int and artifact["id"] > 0
                        and type(artifact["size_in_bytes"]) is int
                        and 0 < artifact["size_in_bytes"] <= MAX_EXTRACTED, "manifest_artifact")
                require(isinstance(artifact["name"], str) and artifact["name"], "manifest_artifact")
                require(re.fullmatch(r"sha256:[a-f0-9]{64}", artifact["digest"] or ""),
                        "missing_pinned_digest")
                inner = artifact["inner_archive"]
                require(safe_name(inner) == inner and "/" not in inner
                        and inner.endswith(".zip" if target == "windows" else ".tar.zst"),
                        "manifest_archive")
                timestamp(artifact["expires_at"])
        source = manifest["sources"][platform]
        require(arch in source["artifacts"], "unsupported_target")
        require(run_id is None or run_id == source["run_id"], "run_id_mismatch")
        pin = {key: value for key, value in source.items() if key != "artifacts"}
        pin["artifact"] = source["artifacts"][arch]
        pin["chromium_version"] = version
        identity = {
            "path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
            "schema_version": 1, "target": f"{platform}-{arch}", "chromium_version": version,
            "repository": pin["repository"], "head_sha": pin["head_sha"],
            "run_id": pin["run_id"], "artifact_id": pin["artifact"]["id"],
            "artifact_digest": pin["artifact"]["digest"],
        }
        return pin, identity
    except (OSError, AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CacheMiss("invalid_manifest_or_pins") from exc


def validate_metadata(pin, run, artifact, now=None):
    now = now or datetime.now(timezone.utc)
    expected = pin["artifact"]
    require(isinstance(run, dict) and isinstance(artifact, dict), "invalid_api_metadata")
    require(all(run.get(key) == pin[key] for key in ("head_sha", "head_branch", "event"))
            and run.get("id") == pin["run_id"]
            and run.get("path") == pin["workflow_path"]
            and run.get("status") == "completed" and run.get("conclusion") == "success",
            "untrusted_run")
    for key in ("repository", "head_repository"):
        repo = run.get(key) or {}
        require(isinstance(repo, dict) and repo.get("full_name") == pin["repository"]
                and repo.get("id") == pin["repository_id"]
                and repo.get("private") is False, "untrusted_repository")
    require(all(artifact.get(key) == expected[key] for key in
                ("id", "name", "size_in_bytes", "digest")), "artifact_mismatch")
    workflow = artifact.get("workflow_run") or {}
    require(isinstance(workflow, dict) and workflow.get("id") == pin["run_id"]
            and workflow.get("head_sha") == pin["head_sha"]
            and workflow.get("head_branch") == pin["head_branch"]
            and workflow.get("repository_id") == pin["repository_id"]
            and workflow.get("head_repository_id") == pin["repository_id"], "artifact_provenance")
    require(artifact.get("expired") is False
            and timestamp(artifact.get("expires_at")) > now, "artifact_expired")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def valid_url(url, api_only=False):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        allowed = host == "api.github.com" if api_only else (
            host == "api.github.com" or host.endswith(".blob.core.windows.net")
            or host.endswith(".actions.githubusercontent.com"))
        require(parsed.scheme == "https" and allowed and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and not parsed.fragment,
                "unsafe_download_url")
    except ValueError as exc:
        raise CacheMiss("unsafe_download_url") from exc


class GitHub:
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN", "")
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, url, download=False):
        valid_url(url, api_only=True)
        for hop in range(6):
            headers = {"User-Agent": OWNER, "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28"}
            # Never carry credentials (or API headers) onto a signed blob redirect.
            if hop == 0 and self.token:
                headers["Authorization"] = "Bearer " + self.token
            if hop:
                headers = {"User-Agent": OWNER}
            try:
                response = self.opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)
                if response.status != 200:
                    response.close()
                    raise CacheMiss("unexpected_http_status")
                return response
            except urllib.error.HTTPError as exc:
                location = exc.headers.get("Location")
                code = exc.code
                exc.close()
                if download and code in (301, 302, 303, 307, 308) and location:
                    url = urllib.parse.urljoin(url, location)
                    valid_url(url)
                    continue
                raise
        raise CacheMiss("too_many_redirects")

    def retry(self, operation):
        for attempt in range(ATTEMPTS):
            try:
                return operation()
            except urllib.error.HTTPError as exc:
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == ATTEMPTS - 1:
                    raise CacheMiss(f"github_http_{exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                if attempt == ATTEMPTS - 1:
                    raise CacheMiss("network_unavailable") from exc
            time.sleep(2**attempt)

    def json(self, path):
        def request():
            with self.open(API + path) as response:
                data = response.read(4 * CHUNK + 1)
                require(len(data) <= 4 * CHUNK, "oversized_api_response")
                try:
                    return json.loads(data)
                except (ValueError, UnicodeError) as exc:
                    raise CacheMiss("invalid_api_response") from exc
        return self.retry(request)

    def download(self, pin, path):
        artifact = pin["artifact"]
        url = f"{API}/repos/{pin['repository']}/actions/artifacts/{artifact['id']}/zip"
        deadline = time.monotonic() + DOWNLOAD_SECONDS

        def request():
            result = hashlib.sha256()
            size = 0
            require(time.monotonic() < deadline, "download_timeout")
            with self.open(url, download=True) as response, path.open("wb") as output:
                while True:
                    require(time.monotonic() < deadline, "download_timeout")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    require(size <= artifact["size_in_bytes"], "download_size_mismatch")
                    result.update(chunk)
                    require_space(path.parent, len(chunk))
                    output.write(chunk)
            if size != artifact["size_in_bytes"]:
                raise http.client.IncompleteRead(b"", artifact["size_in_bytes"] - size)
            require("sha256:" + result.hexdigest() == artifact["digest"], "checksum_mismatch")
            return size
        return self.retry(request)


def safe_name(name, *, posix=False):
    require(isinstance(name, str) and ("\\" not in name or posix and os.name == "posix") and ":" not in name
            and not name.startswith("/") and not any(ord(c) < 32 for c in name), "unsafe_archive_path")
    parts = PurePosixPath(name).parts
    require(".." not in parts, "unsafe_archive_path")
    for part in parts:
        require(not part.endswith((".", " ")) and not re.fullmatch(
            r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part), "unsafe_archive_path")
    return "/".join(parts)


def zip_mtime(info):
    value = calendar.timegm(info.date_time + (0, 0, 0)) * 1_000_000_000
    extra = info.extra
    while len(extra) >= 4:
        kind, length = struct.unpack_from("<HH", extra)
        require(length <= len(extra) - 4, "invalid_zip_timestamp")
        data, extra = extra[4:4 + length], extra[4 + length:]
        if kind == 0x5455 and len(data) >= 5 and data[0] & 1:
            value = struct.unpack_from("<I", data, 1)[0] * 1_000_000_000
        if kind == 0x000A and len(data) >= 4:
            data = data[4:]
            while len(data) >= 4:
                tag, size = struct.unpack_from("<HH", data)
                payload, data = data[4:4 + size], data[4 + size:]
                if tag == 1 and len(payload) >= 8:
                    return (struct.unpack_from("<Q", payload)[0] - 116444736000000000) * 100
    return value


def require_space(path, additional=0):
    require(shutil.disk_usage(path).free >= DISK_HEADROOM + additional, "insufficient_disk_space")


class SourceSelection:
    def __init__(self, source_roots):
        self.trees = tuple(safe_name(root) for root in source_roots)
        require(self.trees and all(self.trees), "invalid_source_roots")
        self.parents = {str(parent) for root in self.trees
                        for parent in PurePosixPath(root).parents if str(parent) != "."}

    def __call__(self, name, kind="file"):
        return (any(name == root or name.startswith(root + "/") for root in self.trees)
                or (kind == "dir" and name in self.parents))


class ToolchainSelection:
    def __init__(self, source_roots):
        self.files = tuple(f"{root}/{name}" for root in source_roots for name in
                           ("BUILD.gn", "chrome/VERSION", "out/Default/args.gn"))
        self.trees = tuple(f"{root}/{name}" for root in source_roots for name in
                          ("tools/clang", "tools/rust", "third_party/llvm-build/Release+Asserts",
                           "third_party/rust-toolchain"))
        self.parents = {str(parent) for name in self.files + self.trees
                        for parent in PurePosixPath(name).parents if str(parent) != "."}

    def __call__(self, name, kind="file"):
        if "__pycache__" in name.split("/") or name.endswith((".pyc", ".pyo")):
            return False
        return (name in self.files or any(name == tree or name.startswith(tree + "/") for tree in self.trees)
                or (kind == "dir" and name in self.parents))


def unpack_outer(outer, inner, expected_name):
    with zipfile.ZipFile(outer) as archive:
        seen = set()
        selected = None
        for info in archive.infolist():
            name = safe_name(info.filename)
            require(name not in seen and len(seen) < MAX_MEMBERS, "duplicate_archive_path")
            seen.add(name)
            mode = info.external_attr >> 16
            require(stat.S_IFMT(mode) in (0, stat.S_IFDIR, stat.S_IFREG), "unsafe_outer_member")
            if name == expected_name:
                require(not info.is_dir() and not info.flag_bits & 1
                        and 0 < info.file_size <= MAX_EXTRACTED, "invalid_inner_archive")
                selected = info
        require(selected is not None, "missing_inner_archive")
        require_space(inner.parent, selected.file_size)
        with archive.open(selected) as source, inner.open("xb") as output:
            while chunk := source.read(CHUNK):
                require_space(inner.parent, len(chunk))
                output.write(chunk)
        require(inner.stat().st_size == selected.file_size, "inner_size_mismatch")
    return inner.stat().st_size


class Extractor:
    """Create regular files first and links last; never traverse archive links."""

    def __init__(self, root, selection=None):
        self.root = root
        self.selection = selection
        self.names = {}
        self.parents = set()
        self.directories = {}
        self.links = []
        self.bytes = 0
        self.archive_bytes = 0
        self.skipped = 0
        self.external_symlinks = []

    def safe_name(self, name):
        return safe_name(name, posix=isinstance(self.selection, SourceSelection))

    def selected(self, name, kind="file"):
        return self.selection is None or self.selection(name, kind)

    def path(self, name):
        path = self.root / name
        for parent in path.parents:
            if parent == self.root:
                break
            require(not parent.is_symlink(), "archive_link_parent")
        return path

    def add(self, name, kind, size, mode, mtime_ns, stream=None, target=None):
        name = self.safe_name(name)
        if not name:
            require(kind == "dir", "empty_archive_path")
            return
        require(name not in self.names and len(self.names) < MAX_MEMBERS, "duplicate_archive_path")
        require(kind in ("file", "dir", "sym", "hard") and size >= 0, "unsupported_archive_member")
        require(kind == "dir" or name not in self.parents, "archive_link_collision")
        for parent in PurePosixPath(name).parents:
            parent = str(parent)
            require(self.names.get(parent, "dir") == "dir", "archive_link_parent")
            self.parents.add(parent)
        self.names[name] = kind
        if kind == "file":
            self.archive_bytes += size
            require(self.archive_bytes <= MAX_EXTRACTED, "archive_too_large")
        if kind in ("sym", "hard"):
            self.links.append((name, kind, target, mode, mtime_ns))
            return
        if not self.selected(name, kind):
            return
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        require(not path.is_symlink(), "archive_link_parent")
        if kind == "dir":
            path.mkdir(exist_ok=True)
            self.directories[name] = (mode, mtime_ns)
        else:
            self.bytes += size
            limit = (MAX_EXTRACTED if self.selection is None or isinstance(self.selection, SourceSelection)
                     else MAX_SELECTED)
            require(self.bytes <= limit, "archive_too_large")
            with path.open("xb") as output:
                remaining = size
                while remaining:
                    chunk = stream.read(min(CHUNK, remaining))
                    require(chunk, "truncated_archive_member")
                    if self.selection:
                        require_space(self.root, len(chunk))
                    output.write(chunk)
                    remaining -= len(chunk)
            self.metadata(path, mode, mtime_ns)

    @staticmethod
    def metadata(path, mode, mtime_ns, symlink=False):
        if not symlink:
            os.chmod(path, mode & 0o777)
        if not symlink or os.utime in os.supports_follow_symlinks:
            os.utime(path, ns=(mtime_ns, mtime_ns), follow_symlinks=not symlink)

    def validate_links(self):
        symlinks = {name: target for name, kind, target, _, _ in self.links if kind == "sym"}
        hardlinks = {}
        for name, kind, target, _, _ in self.links:
            require(target and "\x00" not in target and "\\" not in target
                    and ":" not in target, "unsafe_link")
            if kind == "hard":
                hardlinks[name] = safe_name(target)
                require(hardlinks[name], "unsafe_link")
        for name, target in symlinks.items():
            retained = self.selection is not None and self.selected(name)
            if target.startswith("/"):
                require(not retained or isinstance(self.selection, SourceSelection), "excluded_link_target")
                continue
            pending = deque(PurePosixPath(name).parent.parts + PurePosixPath(target).parts)
            resolved = []
            expansions = 0
            while pending:
                part = pending.popleft()
                if part == "..":
                    require(resolved, "escaping_symlink")
                    resolved.pop()
                    continue
                safe_name(part)
                resolved.append(part)
                link_name = "/".join(resolved)
                link = symlinks.get(link_name)
                if link is not None:
                    require(not link.startswith("/"), "external_symlink_chain")
                    require(not retained or self.selected(link_name), "excluded_link_target")
                    expansions += 1
                    require(expansions <= 128, "cyclic_symlink")
                    resolved.pop()
                    pending.extendleft(reversed(PurePosixPath(link).parts))
            target_name = "/".join(resolved)
            require(not retained or self.selected(target_name), "excluded_link_target")
        for name, target in hardlinks.items():
            seen = {name}
            while True:
                require(target not in seen, "unresolved_hardlink")
                seen.add(target)
                require(not (self.selection and self.selected(name)) or self.selected(target),
                        "excluded_link_target")
                for parent in PurePosixPath(target).parents:
                    require(self.names.get(str(parent), "dir") == "dir", "unsafe_hardlink")
                kind = self.names.get(target)
                if kind != "hard":
                    require(kind == "file", "unsafe_hardlink")
                    break
                target = hardlinks[target]

    def finish(self):
        self.validate_links()
        hard = []
        for name, kind, target, mode, mtime_ns in self.links:
            if kind == "sym" and target.startswith("/"):
                self.skipped += 1
                if self.selected(name):
                    self.external_symlinks.append(name)
                continue
            if not self.selected(name):
                continue
            path = self.path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            require(not path.exists() and not path.is_symlink(), "archive_link_collision")
            if kind == "hard":
                target_name = safe_name(target)
                require(target_name, "unsafe_link")
                hard.append((path, target_name))
                continue
            combined = list(PurePosixPath(name).parent.parts)
            for part in PurePosixPath(target).parts:
                if part == "..":
                    require(combined, "escaping_symlink")
                    combined.pop()
                else:
                    safe_name(part)
                    combined.append(part)
            os.symlink(target, path, target_is_directory=(self.root / "/".join(combined)).is_dir())
            self.metadata(path, mode, mtime_ns, symlink=True)
        while hard:
            pending = []
            for path, target_name in hard:
                target = self.path(target_name)
                require(not target.is_symlink(), "unsafe_hardlink")
                if not target.exists():
                    pending.append((path, target_name))
                    continue
                require(target.is_file(), "unsafe_hardlink")
                os.link(target, path)
            require(len(pending) < len(hard), "unresolved_hardlink")
            hard = pending
        for name in sorted(self.directories, key=lambda n: n.count("/"), reverse=True):
            self.metadata(self.path(name), *self.directories[name])
        return {"extracted_bytes": self.bytes, "members": len(self.names),
                "skipped_external_symlinks": self.skipped,
                "external_symlink_paths": self.external_symlinks}


def extract_tar(stream, tree, selection=None):
    extractor = Extractor(tree, selection)
    with tarfile.open(fileobj=stream, mode="r|", bufsize=CHUNK) as archive:
        for info in archive:
            if info.isdir():
                kind = "dir"
            elif info.issym():
                kind = "sym"
            elif info.islnk():
                kind = "hard"
            else:
                require(info.isfile() and not info.issparse(), "unsupported_archive_member")
                kind = "file"
            mtime = Decimal(info.pax_headers.get("mtime", str(info.mtime)))
            extractor.add(info.name, kind, info.size, info.mode, int(mtime * 1_000_000_000),
                          archive.extractfile(info) if kind == "file" else None, info.linkname)
            # Streaming tarfile otherwise retains every TarInfo in a multi-GB tree.
            archive.members.clear()
    return extractor.finish()


def extract_zip(inner, tree, selection=None):
    extractor = Extractor(tree, selection)
    with zipfile.ZipFile(inner) as archive:
        for info in archive.infolist():
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            require(kind in (0, stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK)
                    and not info.flag_bits & 1, "unsupported_archive_member")
            if info.is_dir():
                extractor.add(info.filename, "dir", 0, mode or 0o755, zip_mtime(info))
            elif kind == stat.S_IFLNK:
                require(info.file_size <= 4096, "oversized_link")
                extractor.add(info.filename, "sym", 0, mode, zip_mtime(info),
                              target=archive.read(info).decode("utf-8"))
            else:
                with archive.open(info) as stream:
                    extractor.add(info.filename, "file", info.file_size, mode or 0o644,
                                  zip_mtime(info), stream)
    return extractor.finish()


def extract_inner(inner, tree, zstd=None, selection=None):
    tree.mkdir()
    if selection:
        require_space(tree)
    if zstd is None:
        return extract_zip(inner, tree, selection)
    with subprocess.Popen([zstd, "-d", "-c", "--", str(inner)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, cwd=inner.parent) as process:
        try:
            result = extract_tar(process.stdout, tree, selection)
            while process.stdout.read(CHUNK):
                pass
            require(process.wait(timeout=TIMEOUT) == 0, "zstd_failed")
            return result
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()


def source_path(tree, pin):
    candidates = []
    for name in pin["source_roots"]:
        path = tree / name
        if (path / "BUILD.gn").is_file():
            candidates.append(path)
    require(len(candidates) == 1, "invalid_source_shape")
    source = candidates[0]
    require(not source.is_symlink() and source.resolve().is_relative_to(tree.resolve()),
            "invalid_source_shape")
    version_file = source / "chrome/VERSION"
    require((source / "BUILD.gn").resolve().is_relative_to(tree.resolve())
            and version_file.resolve().is_relative_to(tree.resolve()), "invalid_source_shape")
    require(version_file.is_file() and version_file.stat().st_size <= 4096, "missing_source_version")
    values = dict(re.findall(r"^(MAJOR|MINOR|BUILD|PATCH)=(\d+)\s*$", version_file.read_text(), re.M))
    require(".".join(values.get(key, "") for key in ("MAJOR", "MINOR", "BUILD", "PATCH"))
            == pin["chromium_version"], "source_version_mismatch")
    return source.resolve()


def write_result(destination, result):
    temporary = destination / ".result.tmp"
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
    temporary.replace(destination / "result.json")


def destination_path(value):
    if not value.strip():
        raise LocalError("destination must be a dedicated directory")
    path = Path(os.path.abspath(Path(value).expanduser()))
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise LocalError("destination cannot contain symlinks or junctions")
    if path == ROOT or path in ROOT.parents or path == Path.home():
        raise LocalError("destination must be a dedicated subdirectory")
    path.mkdir(parents=True, exist_ok=True)
    children = {p.name for p in path.iterdir()}
    previous = None
    if children:
        allowed = {"result.json", "tree", ".download.zip", ".inner", ".result.tmp"}
        if not children <= allowed or "result.json" not in children:
            raise LocalError("destination is neither empty nor result-owned (or is locked)")
        for name in children:
            child = path / name
            if (child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction())
                    or (name == "tree" and not child.is_dir())
                    or (name != "tree" and not child.is_file())):
                raise LocalError("invalid result-owned destination")
        try:
            previous = json.loads((path / "result.json").read_text())
        except (ValueError, UnicodeError) as exc:
            raise LocalError("invalid ownership result") from exc
        if not isinstance(previous, dict) or previous.get("owner") != OWNER \
                or previous.get("destination") != str(path):
            raise LocalError("destination is not owned by this tool")
    return path, previous


def cleanup(destination):
    tree = destination / "tree"
    if tree.exists():
        def writable(function, path, exc):
            os.chmod(path, 0o700)
            function(path)
        shutil.rmtree(tree, onerror=writable)
    for name in (".download.zip", ".inner", ".result.tmp"):
        (destination / name).unlink(missing_ok=True)


def fetch(platform, arch, destination, run_id=None, root=ROOT, client=None):
    started = time.monotonic()
    scope = SOURCE_SCOPE if platform == "linux" else TOOLCHAIN_SCOPE
    destination, previous = destination_path(str(destination))
    lock = destination / ".lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError as exc:
        raise LocalError("destination is locked") from exc
    result = {"owner": OWNER, "status": "miss", "source": None, "destination": str(destination),
              "platform": platform, "arch": arch, "manifest": {"path": str(root / "build/upstream-cache.json")},
              "download_bytes": 0, "inner_bytes": 0, "extracted_bytes": 0,
              "skipped_external_symlinks": 0, "duration_seconds": 0,
              "extraction_scope": scope}
    try:
        if previous is None:
            write_result(destination, result)
        try:
            result["manifest"]["sha256"] = sha256(root / "build/upstream-cache.json").split(":", 1)[1]
            pin, identity = load_manifest(platform, arch, run_id, root)
            result["manifest"] = identity
            if (previous and previous.get("status") == "hit" and previous.get("manifest") == identity
                    and previous.get("extraction_scope") == scope):
                source = source_path(destination / "tree", pin)
                require(previous.get("source") == str(source), "invalid_previous_source")
                return previous
            cleanup(destination)
            write_result(destination, result)
            zstd = None
            if platform != "windows":
                zstd = shutil.which("zstd")
                require(zstd is not None, "zstd_unavailable")
                zstd = str(Path(zstd).resolve())
                require(not Path(zstd).is_relative_to(destination), "unsafe_decompressor")
            client = client or GitHub()
            base = f"/repos/{pin['repository']}/actions"
            run = client.json(f"{base}/runs/{pin['run_id']}")
            artifact = client.json(f"{base}/artifacts/{pin['artifact']['id']}")
            validate_metadata(pin, run, artifact)
            require_space(destination, 2 * pin["artifact"]["size_in_bytes"])
            outer, inner = destination / ".download.zip", destination / ".inner"
            # Download verifies the pinned outer ZIP digest before opening either archive.
            result["download_bytes"] = client.download(pin, outer)
            result["inner_bytes"] = unpack_outer(outer, inner, pin["artifact"]["inner_archive"])
            outer.unlink()
            selection = SourceSelection if platform == "linux" else ToolchainSelection
            result.update(extract_inner(inner, destination / "tree", zstd,
                                        selection(pin["source_roots"])))
            inner.unlink()
            result["source"] = str(source_path(destination / "tree", pin))
            result["status"] = "hit"
        except CacheMiss as exc:
            result["reason"] = str(exc)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, zipfile.LargeZipFile,
                EOFError, UnicodeError, ValueError, OverflowError, InvalidOperation, zlib.error,
                NotImplementedError, subprocess.SubprocessError) as exc:
            result["reason"] = "cache_unusable_" + type(exc).__name__
        if result["status"] == "miss":
            cleanup(destination)
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        write_result(destination, result)
        return result
    finally:
        lock.unlink(missing_ok=True)


def positive_id(value):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("run ID must be a positive integer")
    return int(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=tuple(SOURCES))
    parser.add_argument("--arch", required=True, choices=("x64", "arm64"))
    parser.add_argument("--destination", required=True)
    parser.add_argument("--run-id", type=positive_id, help="must equal the pinned run ID")
    args = parser.parse_args(argv)
    try:
        result = fetch(args.platform, args.arch, args.destination, args.run_id)
    except (LocalError, OSError) as exc:
        print(f"upstream cache: invalid local destination ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    print(f"upstream cache: {result['status']}; skipped external symlinks: "
          f"{result['skipped_external_symlinks']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
