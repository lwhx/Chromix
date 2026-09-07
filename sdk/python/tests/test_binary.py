"""Offline download/extraction integration tests using real ZIP fixtures."""
import hashlib
import io
import os
import stat
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chromix import _binary as binary, api  # noqa: E402

HOST = "https://fixtures.invalid/release"
TAG = binary._CHANNELS["stable"]["tag"]
PLATFORMS = [
    ("Linux", "x86_64", "linux-x64"),
    ("Linux", "aarch64", "linux-arm64"),
    ("Windows", "AMD64", "win-x64"),
    ("Darwin", "x86_64", "mac-x64"),
    ("Darwin", "arm64", "mac-arm64"),
]


def file(name, data=b"fixture", mode=stat.S_IFREG | 0o644):
    return name, data, mode


def link(name, target):
    return file(name, target.encode(), stat.S_IFLNK | 0o777)


def zip_fixture(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return data.getvalue()


def bundle(plat):
    return [file("chromix/", b"", stat.S_IFDIR | 0o755), file(binary._ASSETS[plat][2]),
            file(binary._binary_path(plat, Path(".")).as_posix(), b"chrome fixture"),
            file("chromix/helper", b"helper", stat.S_IFREG | 0o4755),
            file("chromix/resources.pak", b"resources")]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setattr(binary, "_CACHE", root)
    monkeypatch.setattr(api, "_CACHE", root)
    monkeypatch.setenv("CHROMIX_DOWNLOAD_HOST", HOST)
    monkeypatch.delenv("CLOAKBROWSER_BINARY_PATH", raising=False)
    return root


def mock_release(monkeypatch, plat, data, failure=""):
    urls = []

    def retrieve(url, target):
        urls.append(url)
        assert url == f"{HOST}/{binary._ASSETS[plat][0]}"
        if failure == "http":
            raise urllib.error.HTTPError(url, 404, "missing", {}, None)
        if failure == "network":
            raise urllib.error.URLError("network down")
        Path(target).write_bytes(data[:10] if failure == "stream" else data)
        if failure == "stream":
            raise OSError("stream interrupted")
        return target, {}

    def urlopen(url, timeout):
        urls.append(url)
        assert url == f"{HOST}/SHA256SUMS"
        assert timeout == 30
        if failure == "manifest":
            raise urllib.error.HTTPError(url, 404, "missing", {}, None)
        digest = "0" * 64 if failure == "checksum" else hashlib.sha256(data).hexdigest()
        return io.BytesIO(f"{digest.upper()} *{binary._ASSETS[plat][0]}\n".encode())

    monkeypatch.setattr(binary.urllib.request, "urlretrieve", retrieve)
    monkeypatch.setattr(binary.urllib.request, "urlopen", urlopen)
    return urls


@pytest.mark.parametrize("system,machine,plat", PLATFORMS)
def test_zip_download_public_api_and_cache(cache, monkeypatch, system, machine, plat):
    monkeypatch.setattr(binary.platform, "system", lambda: system)
    monkeypatch.setattr(binary.platform, "machine", lambda: machine)
    urls = mock_release(monkeypatch, plat, zip_fixture(bundle(plat)))
    assert binary.resolve_platform() == plat
    assert binary._ASSETS[plat][:2] == (f"chromix-{plat}.zip", "zip")
    assert not api.binary_info(release_channel="stable")["installed"]
    root = cache / TAG / plat
    chrome = binary._binary_path(plat, root)
    assert api.ensure_binary(release_channel="stable") == chrome
    assert chrome.read_bytes() == b"chrome fixture"
    info = api.binary_info(release_channel="stable")
    assert info["installed"] and info["path"] == str(chrome)
    assert binary._download(plat, HOST, TAG) == root / binary._ASSETS[plat][2]
    assert api.ensure_binary(release_channel="stable") == chrome
    assert urls == [f"{HOST}/{binary._ASSETS[plat][0]}", f"{HOST}/SHA256SUMS"]
    assert list((cache / TAG).iterdir()) == [root]
    assert not (root / binary._ASSETS[plat][0]).exists()
    if plat.startswith("mac-"):
        assert chrome.relative_to(root).as_posix() == "chromix/Chromium.app/Contents/MacOS/Chromium"
    if os.name != "nt":
        for path in (chrome, root / binary._ASSETS[plat][2], root / "chromix/helper"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o755
        assert stat.S_IMODE((root / "chromix/resources.pak").stat().st_mode) == 0o644


@pytest.mark.parametrize("system,machine", [("Linux", "i686"), ("Windows", "arm64"), ("Darwin", "ppc"), ("FreeBSD", "amd64")])
def test_unsupported_platform(monkeypatch, system, machine):
    monkeypatch.setattr(binary.platform, "system", lambda: system)
    monkeypatch.setattr(binary.platform, "machine", lambda: machine)
    assert binary.resolve_platform() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_framework_symlinks_and_parent_relative_targets(cache, monkeypatch):
    plat = "mac-arm64"
    entries = bundle(plat) + [
        file("chromix/Framework/Versions/A/Library", b"library", stat.S_IFREG | 0o755),
        link("chromix/Framework/Library", "Versions/Current/Library"),
        link("chromix/Framework/Versions/Current", "A"),
        link("chromix/Framework/chrome", "../Chromium.app/Contents/MacOS/Chromium"),
    ]
    mock_release(monkeypatch, plat, zip_fixture(entries))
    binary._download(plat, HOST, TAG)
    root = cache / TAG / plat
    library = root / "chromix/Framework/Library"
    assert library.is_symlink()
    assert os.readlink(library) == "Versions/Current/Library"
    assert library.read_bytes() == b"library"
    assert (root / "chromix/Framework/chrome").read_bytes() == b"chrome fixture"


@pytest.mark.parametrize("failure", ["http", "network", "stream", "checksum", "corrupt", "missing-launcher", "missing-binary", "directory-binary"])
def test_failure_cleanup_preserves_old_cache_and_retry(cache, monkeypatch, failure):
    plat = "linux-x64"
    root = cache / TAG / plat
    root.mkdir(parents=True)
    marker = root / "old-cache"
    marker.write_text("keep")
    entries = bundle(plat)
    if failure == "missing-launcher":
        entries = [entry for entry in entries if entry[0] != binary._ASSETS[plat][2]]
    if failure in ("missing-binary", "directory-binary"):
        entries = [entry for entry in entries if entry[0] != "chromix/chrome"]
    if failure == "directory-binary":
        entries.append(file("chromix/chrome/", b"", stat.S_IFDIR | 0o755))
    mock_release(monkeypatch, plat, b"not a ZIP" if failure == "corrupt" else zip_fixture(entries), failure)
    with pytest.raises((OSError, RuntimeError, zipfile.BadZipFile)):
        binary._download(plat, HOST, TAG)
    assert marker.read_text() == "keep"
    assert list((cache / TAG).iterdir()) == [root]
    mock_release(monkeypatch, plat, zip_fixture(bundle(plat)))
    binary._download(plat, HOST, TAG)
    assert not marker.exists()
    assert binary._bundle_complete(plat, root)


@pytest.mark.parametrize("missing", ["launcher", "binary", "directory", "external-link"])
def test_public_api_rejects_incomplete_cache(cache, monkeypatch, missing):
    if missing == "external-link" and os.name == "nt":
        pytest.skip("POSIX symlink fixture")
    plat = "linux-x64"
    monkeypatch.setattr(api, "resolve_platform", lambda: plat)
    root = cache / TAG / plat
    launcher = root / binary._ASSETS[plat][2]
    chrome = binary._binary_path(plat, root)
    chrome.parent.mkdir(parents=True)
    if missing != "launcher":
        launcher.write_bytes(b"old")
    if missing == "directory":
        chrome.mkdir()
    elif missing == "external-link":
        outside = cache / "outside"
        outside.write_bytes(b"outside")
        chrome.symlink_to(outside)
    elif missing != "binary":
        chrome.write_bytes(b"old")
    info = api.binary_info(release_channel="stable")
    assert not info["installed"] and info["path"] is None
    urls = mock_release(monkeypatch, plat, zip_fixture(bundle(plat)))
    assert api.ensure_binary(release_channel="stable") == chrome
    assert len(urls) == 2


UNSAFE_ENTRIES = {
    "traversal": [file("chromix/../../outside", b"changed")],
    "absolute": [file("/chromix/outside")],
    "backslash": [file("chromix/..\\outside")],
    "drive": [file("C:/outside")],
    "ads": [file("chromix/chrome:stream")],
    "reserved": [file("chromix/NUL")],
    "trailing": [file("chromix/chrome.")],
    "duplicate": [file("chromix/chrome")],
    "case-alias": [file("chromix/CHROME")],
    "special": [file("chromix/device", b"", stat.S_IFCHR | 0o644)],
    "symlink-write": [link("chromix/link", "../.."), file("chromix/link/outside", b"changed")],
    "symlink-case-write": [link("chromix/Link", "../.."), file("chromix/link/outside", b"changed")],
    "symlink-escape": [link("chromix/link", "../../../outside")],
    "symlink-absolute": [link("chromix/link", "/outside")],
    "symlink-drive": [link("chromix/link", "C:\\outside")],
    "symlink-dangling": [link("chromix/link", "absent")],
    "symlink-cycle": [link("chromix/a", "b"), link("chromix/b", "a")],
    "symlink-chain": [link("chromix/a", "b"), link("chromix/b", "../../../outside")],
    "symlink-root": [link("chromix", "../../outside")],
}


@pytest.mark.parametrize("name,entries", UNSAFE_ENTRIES.items(), ids=UNSAFE_ENTRIES)
@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")
def test_unsafe_zip_cannot_escape_or_publish(cache, monkeypatch, name, entries):
    outside = cache / "outside"
    outside.write_text("untouched")
    mock_release(monkeypatch, "linux-x64", zip_fixture(
        entries if name == "symlink-root" else bundle("linux-x64") + entries))
    with pytest.raises((ValueError, OSError)):
        binary._download("linux-x64", HOST, TAG)
    assert outside.read_text() == "untouched"
    assert list((cache / TAG).iterdir()) == []


def test_missing_manifest_keeps_optional_verification(cache, monkeypatch):
    mock_release(monkeypatch, "linux-x64", zip_fixture(bundle("linux-x64")), "manifest")
    binary._download("linux-x64", HOST, TAG)
    assert binary._bundle_complete("linux-x64", cache / TAG / "linux-x64")
