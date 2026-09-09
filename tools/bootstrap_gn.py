#!/usr/bin/env python3
"""Reuse executable GN or bootstrap it without donor host-build intermediates."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def runnable(path: Path) -> bool:
    if not path.is_file() or not os.access(path, os.X_OK):
        return False
    try:
        result = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def prepare(src: Path, out: Path) -> None:
    src, out = src.absolute(), out.absolute()
    relative = out.relative_to(src)
    if relative.parts not in (("out", "Default"), ("out", "Chromix")):
        raise ValueError("GN output must be src/out/Default or src/out/Chromix")
    for path in (src, src / "out", out):
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"missing or linked GN directory: {path}")
    gn = out / "gn"
    if gn.is_symlink() or (gn.exists() and not gn.is_file()):
        raise ValueError(f"invalid GN output: {gn}")
    if runnable(gn):
        print(f"==> Reusing runnable GN: {gn}", flush=True)
        return
    # The default out/Release/gn_build may contain another host's valid Ninja state.
    build = Path(tempfile.mkdtemp(prefix=".chromix-gn-", dir=src / "out"))
    preserve = False
    try:
        candidate = build / "gn"
        print(f"==> Bootstrapping GN in a fresh host-build directory: {build}", flush=True)
        subprocess.run([sys.executable, "tools/gn/bootstrap/bootstrap.py",
                        "--build-path", str(build.relative_to(src)),
                        "-o", str(candidate), "--skip-generate-buildfiles"],
                       cwd=src, check=True)
        if candidate.is_symlink() or not runnable(candidate):
            raise RuntimeError("bootstrapped GN cannot execute --version on this host")
        previous = build / "previous-gn"
        had_previous = gn.exists()
        if had_previous:
            os.link(gn, previous)
        try:
            os.replace(candidate, gn)
            if not runnable(gn):
                raise RuntimeError("installed GN cannot execute --version on this host")
        except BaseException as failure:
            try:
                if had_previous:
                    os.replace(previous, gn)
                else:
                    gn.unlink(missing_ok=True)
            except BaseException as rollback_error:
                preserve = True
                raise RuntimeError(f"{failure}; GN rollback failed: {rollback_error}; "
                                   f"recovery files preserved at {build}") from failure
            raise
    finally:
        if not preserve:
            shutil.rmtree(build)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.src, args.out)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"GN preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
