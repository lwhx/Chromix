"""Binary management for chromix: download, verify, cache the stealth Chromium bundle.

Detects the platform, downloads the matching bundle from the GitHub Release,
verifies it against SHA256SUMS, and caches it under ``~/.cache/chromix``.
"""
from __future__ import annotations
import hashlib
import os
import platform
import re
import sys
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

_REPO = "xiaozhou26/Chromix"
# Release channels track the latest verified binary for each supported major.
# Source/package versions may move ahead while the staged Chromium build runs.
_CHANNELS = {
    "stable": {"tag": "v151.0.7922.173"},
    "latest": {"tag": "v152.0.7977.75"},
}
_CACHE = Path(os.environ.get("CHROMIX_CACHE_DIR",
                             Path.home() / ".cache" / "chromix"))


def _host(tag: str) -> str:
    return os.environ.get("CHROMIX_DOWNLOAD_HOST",
                          f"https://github.com/{_REPO}/releases/download/{tag}")


# platform key -> (release asset, archive kind, launcher relative path)
_ASSETS = {
    "linux-x64":   ("chromix-linux-x64.zip",   "zip", "chromix/chromix"),
    "linux-arm64": ("chromix-linux-arm64.zip", "zip", "chromix/chromix"),
    "win-x64":     ("chromix-win-x64.zip",     "zip", "chromix/chromix.cmd"),
    "mac-arm64":   ("chromix-mac-arm64.zip",   "zip", "chromix/chromix"),
    "mac-x64":     ("chromix-mac-x64.zip",     "zip", "chromix/chromix"),
}


def resolve_platform() -> str | None:
    sysname, mach = platform.system(), platform.machine().lower()
    if sysname == "Linux" and mach in ("x86_64", "amd64"):
        return "linux-x64"
    if sysname == "Linux" and mach in ("aarch64", "arm64"):
        return "linux-arm64"
    if sysname == "Windows" and mach in ("amd64", "x86_64"):
        return "win-x64"
    if sysname == "Darwin" and mach in ("arm64", "aarch64"):
        return "mac-arm64"
    if sysname == "Darwin" and mach in ("x86_64", "amd64"):
        return "mac-x64"
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _expected_sha(asset: str, host: str) -> str | None:
    """Fetch SHA256SUMS from the release and return the hash for `asset`."""
    try:
        with urllib.request.urlopen(f"{host}/SHA256SUMS", timeout=30) as r:
            for line in r.read().decode().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("*") == asset:
                    return parts[0].lower()
    except Exception:
        return None
    return None


def _binary_path(plat: str, root: Path) -> Path:
    if plat.startswith("mac-"):
        return root / "chromix/Chromium.app/Contents/MacOS/Chromium"
    return root / "chromix" / ("chrome.exe" if plat == "win-x64" else "chrome")


def _bundle_complete(plat: str, root: Path) -> bool:
    if plat not in _ASSETS:
        return False
    try:
        bundle = root.resolve(strict=True) / "chromix"
        for path in (root / _ASSETS[plat][2], _binary_path(plat, root)):
            path.resolve(strict=True).relative_to(bundle)
            if not path.is_file():
                return False
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def _extract_zip(archive: Path, root: Path) -> None:
    """Extract bundle files and internal symlinks, preserving executable bits."""
    with zipfile.ZipFile(archive) as z:
        entries = []
        names = set()
        links = set()
        for entry in z.infolist():
            filename = entry.orig_filename
            parts = (filename[:-1] if filename.endswith("/") else filename).split("/")
            name = PurePosixPath(*parts)
            # Reject Windows aliases too, even when extracting on POSIX.
            if (parts[0] != "chromix" or any(
                    not part or part.endswith((".", " "))
                    or re.search(r'[\\\\:\x00-\x1f<>"|?*]', part)
                    or re.match(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part)
                    for part in parts)):
                raise ValueError(f"Unsafe ZIP path: {entry.orig_filename}")
            key = str(name).lower()
            if key in names:
                raise ValueError(f"Duplicate ZIP path: {name}")
            names.add(key)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                links.add(key)
            elif stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"Unsupported ZIP entry: {name}")
            entries.append((entry, name, mode))
        for _, name, _ in entries:
            if any(str(parent).lower() in links for parent in name.parents):
                raise ValueError(f"ZIP entry traverses a symlink: {name}")
        for entry, name, mode in entries:
            target = root.joinpath(*name.parts)
            if stat.S_ISLNK(mode):
                continue
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(entry) as source, target.open("xb") as dest:
                    shutil.copyfileobj(source, dest)
                if os.name != "nt":
                    target.chmod(0o755 if mode & 0o111 else 0o644)
        for entry, name, mode in entries:
            if not stat.S_ISLNK(mode):
                continue
            target = root.joinpath(*name.parts)
            if entry.file_size > 4096:
                raise ValueError(f"Unsafe ZIP symlink: {name}")
            link = z.read(entry).decode("utf-8")
            if (not link or "\\" in link or ":" in link or "\x00" in link
                    or PurePosixPath(link).is_absolute()):
                raise ValueError(f"Unsafe ZIP symlink: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
        bundle = root.resolve(strict=True) / "chromix"
        for _, name, mode in entries:
            if stat.S_ISLNK(mode):
                try:
                    root.joinpath(*name.parts).resolve(strict=True).relative_to(bundle)
                except (ValueError, OSError, RuntimeError) as error:
                    raise ValueError(f"Unsafe ZIP symlink: {name}") from error


def _download(plat: str, host: str, tag: str) -> Path:
    """Download and verify into a temporary tree before publishing the cache."""
    asset, _, launcher_rel = _ASSETS[plat]
    root = _CACHE / tag / plat
    launcher = root / launcher_rel
    if _bundle_complete(plat, root):
        return launcher
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{plat}-extract-", dir=root.parent))
    try:
        archive = stage / asset
        url = f"{host}/{asset}"
        sys.stderr.write(f"[chromix] downloading {url} ...\n")
        urllib.request.urlretrieve(url, archive)
        expected = _expected_sha(asset, host)
        if expected:
            actual = _sha256(archive)
            if actual != expected:
                raise RuntimeError(f"SHA256 mismatch for {asset}: expected {expected}, got {actual}")
            sys.stderr.write("[chromix] SHA256 verified\n")
        else:
            sys.stderr.write("[chromix] WARNING: no SHA256SUMS published; skipping verification\n")
        _extract_zip(archive, stage)
        if not _bundle_complete(plat, stage):
            raise RuntimeError("bundle extracted but launcher or chrome binary missing")
        if os.name != "nt":
            (stage / launcher_rel).chmod(0o755)
            _binary_path(plat, stage).chmod(0o755)
        archive.unlink()
        if root.is_symlink() or root.is_file():
            root.unlink()
        elif root.exists():
            shutil.rmtree(root)
        stage.rename(root)
        return launcher
    finally:
        if stage.exists():
            shutil.rmtree(stage)
