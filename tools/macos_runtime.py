#!/usr/bin/env python3
"""Source-confined loader paths for the pinned macOS LLVM and nightly Rust layout."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import tempfile

LIBRARY_DIRS = (
    "third_party/llvm-build/Release+Asserts/lib",
    "third_party/rust-toolchain/lib",
    "third_party/rust-toolchain/rustc/lib",
)
TRIPLES = {"arm64": "aarch64-apple-darwin", "x64": "x86_64-apple-darwin"}
CPUS = {"arm64": 0x100000C, "x64": 0x1000007}
BINDGEN_WRAPPER = "build/rust/gni_impl/run_bindgen.py"
BINDGEN_ORIGINAL_SHA256 = "53c0e089ad4cef4f718faccccbc3a139a38eca97773e0530b8b4f989010b5a82"
BINDGEN_PATCHED_SHA256 = "2d33e7077b2472aa701d503d4cfa5df6470e4d5a22377f8dbf59f4df1e840143"
BINDGEN_ORIGINAL_ENV = b'''    env = os.environ
    if args.ld_library_path:
      if sys.platform == 'darwin':
        env["DYLD_LIBRARY_PATH"] = args.ld_library_path
'''
BINDGEN_SCOPED_ENV = b'''    env = os.environ.copy()
    if sys.platform == 'darwin':
      env = {key: value for key, value in env.items()
             if not key.startswith('DYLD_')}
    if args.ld_library_path:
      if sys.platform == 'darwin':
        # Expose libclang without overriding the system C++ runtime.
        library_path = stack.enter_context(
            tempfile.TemporaryDirectory(prefix='chromix-bindgen-'))
        os.symlink(os.path.realpath(os.path.join(args.ld_library_path,
                                                'libclang.dylib')),
                   os.path.join(library_path, 'libclang.dylib'))
        env["DYLD_LIBRARY_PATH"] = library_path
'''


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
    """Return a clean probe/build environment without advertising toolchain libs."""
    _source_root(src, arch)
    return {key: value for key, value in (os.environ if environ is None else environ).items()
            if not key.startswith("DYLD_")}


@contextmanager
def bindgen_environment(src: Path, arch: str, environ=None):
    """Expose only native libclang for the lifetime of a bindgen child."""
    root = _source_root(src, arch)
    env = runtime_environment(root, arch, environ)
    for relative in (LIBRARY_DIRS[1], LIBRARY_DIRS[0]):
        library = _inside(root, root / relative / "libclang.dylib")
        if not _native(library, arch):
            continue
        with tempfile.TemporaryDirectory(prefix="chromix-bindgen-") as directory:
            view = Path(directory).resolve()
            if any(c in str(view) for c in (":", "\n", "\r", "\0")):
                raise ValueError(f"unsafe macOS libclang view: {view}")
            (view / "libclang.dylib").symlink_to(library)
            env["DYLD_LIBRARY_PATH"] = str(view)
            yield env
        return
    yield env


def repair_bindgen_wrapper(src: Path, arch: str) -> None:
    """Repair only the pinned wrapper, accepting its exact patched state on resume."""
    root = _source_root(src, arch)
    path = root / BINDGEN_WRAPPER
    _inside(root, path)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError(f"linked bindgen wrapper: {path}")
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest == BINDGEN_PATCHED_SHA256:
        return
    if digest != BINDGEN_ORIGINAL_SHA256:
        raise ValueError(f"unknown bindgen wrapper SHA256: {digest}")
    if original.count(BINDGEN_ORIGINAL_ENV) != 1 or original.count(b"import sys\n") != 1:
        raise ValueError("pinned bindgen wrapper block mismatch")
    patched = original.replace(b"import sys\n", b"import sys\nimport tempfile\n")
    patched = patched.replace(BINDGEN_ORIGINAL_ENV, BINDGEN_SCOPED_ENV)
    if hashlib.sha256(patched).hexdigest() != BINDGEN_PATCHED_SHA256:
        raise ValueError("patched bindgen wrapper SHA256 mismatch")
    path.write_bytes(patched)


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
    parser.add_argument("--repair-bindgen-wrapper", action="store_true")
    args = parser.parse_args(argv)
    try:
        runtime_environment(args.src, args.arch)
        if args.prepare_loader:
            prepare_runtime_loader(args.src, args.arch)
        if args.repair_bindgen_wrapper:
            repair_bindgen_wrapper(args.src, args.arch)
        if args.verify_loader:
            verify_runtime_loader(args.src, args.arch)
        # Scrub in the parent shell even if SIP hid its DYLD_* from Python.
        print('unset -- "${!DYLD_@}"')
    except (OSError, ValueError, RuntimeError) as error:
        print(f"macOS runtime preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
