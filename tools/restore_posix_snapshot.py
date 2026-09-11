#!/usr/bin/env python3
"""Extract a POSIX checkpoint before publishing it, retaining the download cache."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from download_posix_snapshot import publish
from snapshot_volumes import volume_paths


def extract(volumes, destination):
    processes = []
    try:
        reader = subprocess.Popen(["cat", *volumes], stdout=subprocess.PIPE)
        processes.append(reader)
        decoder = subprocess.Popen(["zstd", "-d", "-T0"], stdin=reader.stdout,
                                   stdout=subprocess.PIPE)
        processes.append(decoder)
        reader.stdout.close()
        unpacker = subprocess.Popen(["tar", "-xpf", "-", "-C", str(destination)],
                                    stdin=decoder.stdout)
        processes.append(unpacker)
        decoder.stdout.close()
        statuses = [process.wait() for process in reversed(processes)]
        if any(statuses):
            raise RuntimeError(f"snapshot extraction failed (tar, zstd, cat: {statuses})")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()


def restore(source, destination):
    source = Path(source)
    destination = Path(os.path.abspath(destination))
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("snapshot and destination must not be symlinks")
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if (source == destination or source in destination.parents
            or destination in source.parents):
        raise ValueError("snapshot and destination must not overlap")
    if destination.exists() and not destination.is_dir():
        raise ValueError("destination is not a directory")
    volumes = volume_paths(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-",
                                    dir=destination.parent))
    tree = staging / "tree"
    old = staging / "old"
    tree.mkdir()
    committed = False
    rollback_ok = True
    try:
        extract(volumes, tree)
        if os.path.lexists(tree / "download_cache"):
            raise ValueError("snapshot must exclude the root download_cache")
        root_metadata = tree.stat()
        # Defer termination across the rename/rollback critical section. This
        # prevents a signal between a successful rename and its Python state
        # update from treating a published tree as an unpublished one.
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK,
                                               {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
        try:
            if destination.exists():
                publish(destination, old)
            if os.path.lexists(old / "download_cache"):
                publish(old / "download_cache", tree / "download_cache")
                os.utime(tree, ns=(root_metadata.st_atime_ns, root_metadata.st_mtime_ns))
            publish(tree, destination)
            committed = True
        finally:
            try:
                if not committed:
                    try:
                        if os.path.lexists(tree / "download_cache"):
                            publish(tree / "download_cache", old / "download_cache")
                        if old.exists():
                            publish(old, destination)
                    except BaseException:
                        rollback_ok = False
                        print(f"Restore rollback incomplete; original data retained at {staging}",
                              file=sys.stderr)
                        raise
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    finally:
        if rollback_ok:
            shutil.rmtree(staging)
    # A corrupt stream never reaches publication or deletes its input volumes.
    shutil.rmtree(source)


def interrupted(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    for name in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(name, interrupted)
    try:
        restore(args.snapshot, args.destination)
    except (OSError, RuntimeError, ValueError, KeyboardInterrupt) as error:
        parser.exit(1, f"snapshot restore failed: {error}\n")


if __name__ == "__main__":
    main()
