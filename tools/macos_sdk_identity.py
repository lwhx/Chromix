"""Content identities for complete, self-contained macOS SDK trees."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat


_STAT_FIELDS = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
_FORMAT = {"schema_version": 1, "algorithm": "sha256", "scope": "sdk-tree-v1"}


def validated_sdk_content(value: object) -> bool:
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or any(value.get(key) != expected for key, expected in _FORMAT.items())
            or value.get("complete") is not True
            or not isinstance(value.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None):
        return False
    return all(type(value.get(key)) is int and value[key] >= minimum
               for key, minimum in (("files", 1), ("directories", 1), ("symlinks", 0), ("bytes", 0)))


def sdk_content_identity(path: Path) -> dict:
    """Hash every entry; unverifiable links or concurrent edits disable reuse."""
    digest = hashlib.sha256()
    counts = dict(files=0, directories=0, symlinks=0, bytes=0)
    file_hashes = {}
    links = []
    entries_seen = {}

    def signature(info):
        return tuple(getattr(info, key) for key in _STAT_FIELDS)

    def unchanged(entry, before):
        if signature(entry.lstat()) != signature(before):
            raise ValueError(f"SDK entry changed during hashing: {entry}")

    def record(value):
        digest.update(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")

    def file_digest(entry, before):
        key = signature(before)
        if key not in file_hashes:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(os.open(entry, flags), "rb") as stream:
                if signature(os.fstat(stream.fileno())) != key:
                    raise ValueError(f"SDK file changed before hashing: {entry}")
                content = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    content.update(chunk)
                if signature(os.fstat(stream.fileno())) != key:
                    raise ValueError(f"SDK file changed during hashing: {entry}")
            if before.st_nlink > 1:
                file_hashes[key] = content.hexdigest()
            value = content.hexdigest()
        else:
            value = file_hashes[key]
        unchanged(entry, before)
        return value

    def walk(directory, before):
        relative = directory.relative_to(root).as_posix()
        entries_seen[directory] = before
        record([relative, "directory", stat.S_IMODE(before.st_mode)])
        counts["directories"] += 1
        with os.scandir(directory) as stream:
            entries = sorted(stream, key=lambda entry: entry.name)
        for entry in entries:
            child = Path(entry.path)
            info = entry.stat(follow_symlinks=False)
            relative = child.relative_to(root).as_posix()
            entries_seen[child] = info
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(child)
                resolved = child.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    raise ValueError(f"SDK symlink target is outside the hashed tree: {child}")
                links.append((child, info, resolved))
                record([relative, "symlink", target, resolved.relative_to(root).as_posix()])
                counts["symlinks"] += 1
            elif stat.S_ISDIR(info.st_mode):
                walk(child, info)
            elif stat.S_ISREG(info.st_mode):
                record([relative, "file", stat.S_IMODE(info.st_mode), info.st_size, file_digest(child, info)])
                counts["files"] += 1
                counts["bytes"] += info.st_size
            else:
                raise ValueError(f"unsupported SDK entry: {child}")
        unchanged(directory, before)

    try:
        root = path.resolve(strict=True)
        before = root.lstat()
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError(f"SDK root is not a directory: {path}")
        walk(root, before)
        # Internal aliases are already covered; never expand cycles or external trees.
        for link, info, resolved in links:
            target = link.resolve(strict=True)
            if target != resolved or target not in entries_seen:
                raise ValueError(f"SDK symlink target changed or was not hashed: {link}")
            unchanged(link, info)
        # Recheck earlier files as well as the directory currently being walked.
        for entry, info in entries_seen.items():
            unchanged(entry, info)
        if path.resolve(strict=True) != root:
            raise ValueError(f"SDK root changed during hashing: {path}")
        if not counts["files"]:
            raise ValueError(f"SDK tree has no files: {path}")
    except (OSError, ValueError, RuntimeError) as error:
        return dict(_FORMAT, complete=False, error=str(error))
    return dict(_FORMAT, complete=True, sha256=digest.hexdigest(), **counts)
