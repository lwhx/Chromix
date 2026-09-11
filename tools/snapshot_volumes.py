#!/usr/bin/env python3
"""Emit validated POSIX snapshot volumes in archive order."""
import os
import re
import stat
import sys


PATTERN = re.compile(r"^tree\.tar\.zst\.([0-9]{3})$")


def fail(message):
    print(f"snapshot volume error: {message}", file=sys.stderr)
    raise SystemExit(1)


def volume_paths(directory):
    root = os.path.abspath(directory)
    if not os.path.isdir(root):
        fail(f"snapshot directory does not exist: {directory}")
    volumes = {}
    def walk_error(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs + files:
            match = PATTERN.fullmatch(name)
            if match is None:
                if name.startswith("tree.tar.zst"):
                    fail(f"invalid volume name: {name}")
                if name in dirs and os.path.islink(os.path.join(directory, name)):
                    fail(f"symlink volume directory: {name}")
                continue
            path = os.path.join(directory, name)
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode):
                fail(f"volume is not a regular file: {path}")
            number = int(match.group(1))
            if number == 0:
                fail(f"volume numbering starts at zero: {path}")
            if number in volumes:
                fail(f"duplicate volume number {number:03d}")
            if metadata.st_size == 0:
                fail(f"empty volume: {path}")
            volumes[number] = path
    if not volumes:
        fail(f"snapshot has no tree archive: {root}")
    expected = set(range(1, len(volumes) + 1))
    actual = set(volumes)
    if actual != expected:
        missing = ",".join(f"{item:03d}" for item in sorted(expected - actual))
        extra = ",".join(f"{item:03d}" for item in sorted(actual - expected))
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unexpected {extra}")
        fail("noncontiguous volumes (" + "; ".join(detail) + ")")
    return [volumes[number] for number in sorted(volumes)]


def main(argv):
    if len(argv) != 2:
        fail("usage: snapshot_volumes.py SNAPSHOT_DIR")
    paths = volume_paths(argv[1])
    for path in paths:
        sys.stdout.buffer.write(os.fsencode(path) + b"\0")


if __name__ == "__main__":
    main(sys.argv)
