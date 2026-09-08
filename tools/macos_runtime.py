#!/usr/bin/env python3
"""Source-confined loader paths for the pinned macOS LLVM and nightly Rust layout."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shlex
import struct
import subprocess
import sys

LIBRARY_DIRS = (
    "third_party/llvm-build/Release+Asserts/lib",
    "third_party/rust-toolchain/lib",
    "third_party/rust-toolchain/rustc/lib",
)
TRIPLES = {"arm64": "aarch64-apple-darwin", "x64": "x86_64-apple-darwin"}
CPUS = {"arm64": 0x100000C, "x64": 0x1000007}


def _source_root(src: Path, arch: str) -> Path:
    if arch not in TRIPLES:
        raise ValueError(f"unsupported macOS runtime architecture: {arch}")
    src = Path(src).absolute()
    if src.is_symlink():
        raise ValueError(f"linked macOS runtime source root: {src}")
    root = src.resolve(strict=True)
    if not root.is_dir() or any(c in str(root) for c in (":", "\n", "\r", "\0")):
        raise ValueError(f"unsafe macOS runtime source root: {src}")
    return root


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"macOS runtime path escapes source: {path}")
    if any(c in str(resolved) for c in (":", "\n", "\r", "\0")):
        raise ValueError(f"unsafe macOS runtime path: {path}")
    return resolved


def _native(path: Path, arch: str) -> bool:
    """Read Mach-O CPU headers without executing restored code."""
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        header = stream.read(8)
        if len(header) != 8:
            return False
        if header[:4] == b"\xcf\xfa\xed\xfe":
            return struct.unpack_from("<I", header, 4)[0] == CPUS[arch]
        if header[:4] not in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
            return False
        count = struct.unpack_from(">I", header, 4)[0]
        if count > 32:
            return False
        width = 32 if header[:4] == b"\xca\xfe\xba\xbf" else 20
        entries = stream.read(count * width)
        return len(entries) == count * width and any(
            struct.unpack_from(">I", entries, offset)[0] == CPUS[arch]
            for offset in range(0, len(entries), width))


def runtime_environment(src: Path, arch: str, environ=None) -> dict[str, str]:
    """Return a probe/build environment; never mutate the source or caller's env."""
    root = _source_root(src, arch)
    directories = []
    for relative in LIBRARY_DIRS:
        directory = _inside(root, root / relative)
        if not directory.is_dir():
            continue
        libraries = [_inside(root, path) for path in sorted(directory.glob("*.dylib"))]
        if libraries and all(_native(path, arch) for path in libraries):
            if str(directory) not in directories:
                directories.append(str(directory))
    # Do not inherit loader overrides, fallback paths, or injection settings.
    env = {key: value for key, value in (os.environ if environ is None else environ).items()
           if not key.startswith("DYLD_")}
    if directories:
        env["DYLD_LIBRARY_PATH"] = ":".join(directories)
    return env


def _loader_paths(root: Path, arch: str) -> tuple[Path, Path, Path]:
    rustc = root / "third_party/rust-toolchain/rustc"
    library = _inside(root, rustc / "lib/libLLVM.dylib")
    objcopy = _inside(root, rustc / f"lib/rustlib/{TRIPLES[arch]}/bin/rust-objcopy")
    for path in (library, objcopy):
        if not _native(path, arch):
            raise ValueError(f"missing or non-native {arch} nightly Rust runtime: {path}")
    directory = _inside(root, rustc / f"lib/rustlib/{TRIPLES[arch]}/lib")
    if not directory.is_dir():
        raise ValueError(f"missing nightly Rust runtime library directory: {directory}")
    return library, objcopy, directory / "libLLVM.dylib"


def prepare_runtime_loader(src: Path, arch: str) -> None:
    """Supply rust-objcopy's existing @loader_path/../lib search location."""
    root = _source_root(src, arch)
    library, _, destination = _loader_paths(root, arch)
    resolved = _inside(root, destination)
    if destination.exists() or destination.is_symlink():
        if resolved != library and not _native(resolved, arch):
            raise ValueError(f"invalid existing Rust LLVM runtime: {destination}")
        return
    # Ninja's /bin/sh hop can drop DYLD_*; keep this fix relative and relocatable.
    destination.symlink_to(os.path.relpath(library, destination.parent))


def verify_runtime_loader(src: Path, arch: str) -> None:
    """Probe the repaired nightly loader without environment-based resolution."""
    root = _source_root(src, arch)
    machine = {"x86_64": "x64", "arm64": "arm64"}.get(platform.machine())
    if platform.system() != "Darwin" or machine != arch:
        raise ValueError(f"a native macOS {arch} host is required for the Rust LLVM loader probe")
    _, objcopy, destination = _loader_paths(root, arch)
    if not _native(_inside(root, destination), arch):
        raise ValueError(f"missing or non-native repaired Rust LLVM runtime: {destination}")
    if not os.access(objcopy, os.X_OK):
        raise ValueError(f"nightly rust-objcopy is not executable: {objcopy}")
    env = {key: value for key, value in os.environ.items() if not key.startswith("DYLD_")}
    try:
        result = subprocess.run([str(objcopy), "--version"], cwd=root, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", timeout=10, check=False)
    except subprocess.TimeoutExpired as error:
        output = error.output or b""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        print(f"rust-objcopy --version without DYLD_* timed out: {output[:2000]}", file=sys.stderr)
        raise ValueError("Rust LLVM loader probe timed out after 10 seconds") from error
    print(f"rust-objcopy --version without DYLD_* (exit {result.returncode}): "
          f"{result.stdout[:2000].strip()}", file=sys.stderr)
    if result.returncode:
        raise ValueError(f"Rust LLVM loader probe exited {result.returncode}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--arch", choices=tuple(TRIPLES), required=True)
    parser.add_argument("--prepare-loader", action="store_true")
    parser.add_argument("--verify-loader", action="store_true")
    args = parser.parse_args(argv)
    try:
        env = runtime_environment(args.src, args.arch)
        if args.prepare_loader:
            prepare_runtime_loader(args.src, args.arch)
        if args.verify_loader:
            verify_runtime_loader(args.src, args.arch)
        inherited = sorted(key for key in os.environ if key.startswith("DYLD_") and key != "DYLD_LIBRARY_PATH")
        if inherited:
            print("unset -- " + " ".join(shlex.quote(key) for key in inherited))
        value = env.get("DYLD_LIBRARY_PATH")
        print("export DYLD_LIBRARY_PATH=" + shlex.quote(value) if value else "unset DYLD_LIBRARY_PATH")
    except (OSError, ValueError, RuntimeError) as error:
        print(f"macOS runtime preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
