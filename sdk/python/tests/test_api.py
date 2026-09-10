"""API tests for the CloakBrowser-compatible chromix surface (no browser launch needed)."""
import asyncio
import errno
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chromix import api  # noqa: E402
from chromix.humanize import Humanizer, patch_page, resolve_human_config  # noqa: E402


class _MockMouse:
    def __init__(self):
        self.moves, self.wheels = [], []

    def move(self, x, y, steps=None, **kw):
        self.moves.append((x, y))

    def down(self, button="left"):
        pass

    def up(self, button="left"):
        pass

    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))


class _MockKeyboard:
    def __init__(self):
        self.typed = []

    def type(self, text, delay=0, **kw):
        self.typed.append(text)

    def press(self, key, **kw):
        self.typed.append(f"<{key}>")


class _MockPage:
    def __init__(self):
        self.mouse, self.keyboard = _MockMouse(), _MockKeyboard()


def test_cloak_api_import_surface():
    import chromix as cx
    for name in api.__all__:
        assert getattr(cx, name, None) is not None, name


def test_proxy_string_with_credentials():
    pw, extra = api._resolve_proxy_config("http://u:p@proxy:8080")
    assert pw == {"proxy": {"server": "http://proxy:8080", "username": "u", "password": "p"}}
    assert extra == []


def test_proxy_string_plain():
    pw, _ = api._resolve_proxy_config("socks5://proxy:1080")
    assert pw == {"proxy": {"server": "socks5://proxy:1080"}}


def test_proxy_dict_passthrough():
    pw, _ = api._resolve_proxy_config(
        {"server": "http://proxy:8080", "bypass": ".google.com", "username": "u"})
    assert pw == {"proxy": {"server": "http://proxy:8080",
                            "bypass": ".google.com", "username": "u"}}


def test_extract_proxy_url():
    assert api._extract_proxy_url(None) is None
    assert api._extract_proxy_url("http://p:1") == "http://p:1"
    assert api._extract_proxy_url(
        {"server": "http://p:1", "username": "u", "password": "p"}) == "http://u:p@p:1"


def test_build_args_priority_and_dedupe():
    args = api.build_args(
        True,
        ["--fingerprint=42", "--lang=fr-FR", "--window-size=800,600"],
        timezone="Europe/Paris", locale="en-US", headless=True)
    # user overrides stealth seed; dedicated locale param overrides user lang
    assert "--fingerprint=42" in args
    assert "--lang=en-US" in args
    assert "--lang=fr-FR" not in args
    assert "--fingerprint-locale=en-US" in args
    assert "--fingerprint-timezone=Europe/Paris" in args
    assert "--window-size=800,600" in args
    # no duplicates by flag key
    keys = [a.split("=", 1)[0] for a in args]
    assert len(keys) == len(set(keys))


def test_build_args_maximize_suppressed_by_geometry():
    args = api.build_args(True, ["--window-size=800,600"], start_maximized=True)
    assert "--start-maximized" not in args
    args = api.build_args(True, None, start_maximized=True)
    assert "--start-maximized" in args


def test_get_default_stealth_args_seed_shape():
    sa = api.get_default_stealth_args()
    assert "--no-sandbox" not in sa
    seeds = [a for a in sa if a.startswith("--fingerprint=")]
    assert len(seeds) == 1 and 1 <= int(seeds[0].split("=")[1]) <= 0xFFFFFFFF


def test_geoip_disabled_passthrough():
    assert api.maybe_resolve_geoip(False, None, "UTC", "en") == ("UTC", "en", None)


def test_geoip_flag_promotion(monkeypatch):
    calls = {}

    def fake_http(proxy_url):
        calls["proxy"] = proxy_url
        return ("Asia/Tokyo", "jp", "203.0.113.9")

    monkeypatch.setattr(api, "_geoip_http", fake_http)
    # Raw flags count as explicit: geoip must not clobber them.
    tz, loc, ip = api.maybe_resolve_geoip(
        True, "http://p:1", None, None,
        ["--fingerprint-timezone=Europe/Berlin", "--lang=de-DE"])
    assert (tz, loc) == ("Europe/Berlin", "de-DE")
    assert ip == "203.0.113.9"
    assert calls["proxy"] == "http://p:1"


def test_webrtc_auto_resolution(monkeypatch):
    monkeypatch.setattr(api, "_geoip_http", lambda p: (None, None, "198.51.100.7"))
    out = api._resolve_webrtc_args(["--fingerprint-webrtc-ip=auto"], "http://p:1")
    assert out == ["--fingerprint-webrtc-ip=198.51.100.7"]
    out = api._resolve_webrtc_args(["--fingerprint-webrtc-ip=auto"], None)
    assert out == []


def test_webrtc_exit_ip_append():
    out = api._append_webrtc_exit_ip(["--headless=new"], "198.51.100.7")
    assert out[-1] == "--fingerprint-webrtc-ip=198.51.100.7"
    out = api._append_webrtc_exit_ip(["--fingerprint-webrtc-ip=x"], "198.51.100.7")
    assert out == ["--fingerprint-webrtc-ip=x"]
    assert api._append_webrtc_exit_ip(None, None) is None


def test_human_config_presets_and_overrides():
    cfg = resolve_human_config()
    slow = resolve_human_config("careful")
    assert slow.typing_delay > cfg.typing_delay
    custom = resolve_human_config("default", {"mistype_chance": 0.5, "seed": 7})
    assert custom.mistype_chance == 0.5 and custom.seed == 7


def test_patch_page_wraps_mouse_and_keyboard():
    page = _MockPage()
    cfg = resolve_human_config("default", {"seed": 1})
    # Make it fast: shrink all delays.
    cfg.typing_delay, cfg.typing_delay_spread = 0, 0
    cfg.typing_pause_chance, cfg.mistype_chance = 0, 0
    cfg.click_aim_delay, cfg.click_hold = 0, 0
    patch_page(page, cfg)
    page.mouse.click(100, 100)
    assert page.mouse.moves, "click must move the cursor along a path"
    page.keyboard.type("hi")
    assert page.keyboard.typed == ["h", "i"]
    page.mouse.wheel(0, 60)
    assert any(dy > 0 for _, dy in page.mouse.wheels), \
        "scroll must emit downward wheel events"


def test_humanizer_zero_sleep_fast_path():
    page = _MockPage()
    h = Humanizer(page, cfg=resolve_human_config("default", {"seed": 3}))
    h.cfg.typing_delay = h.cfg.typing_delay_spread = 0
    h.cfg.typing_pause_chance = h.cfg.mistype_chance = 0
    t0 = time.monotonic()
    h.type("abc").click(10, 10)
    assert time.monotonic() - t0 < 1.0


@pytest.mark.parametrize("platform,persona", [
    ("linux", "linux"), ("win32", "windows"), ("darwin", "macos"), ("freebsd", None),
])
def test_native_platform_defaults_and_override(monkeypatch, platform, persona):
    monkeypatch.setattr(api.sys, "platform", platform)
    defaults = api.get_default_stealth_args()
    platforms = [arg for arg in defaults if arg.startswith("--fingerprint-platform=")]
    assert platforms == ([f"--fingerprint-platform={persona}"] if persona else [])
    args = api.build_args(True, ["--fingerprint-platform=macos"])
    assert [a for a in args if a.startswith("--fingerprint-platform=")] == ["--fingerprint-platform=macos"]
    assert not any(a.startswith("--fingerprint") for a in api.build_args(False, None))


SEED_FILE = ".chromix-fingerprint-seed"


def fingerprint(args):
    seeds = [a.split("=", 1)[1] for a in args if a.startswith("--fingerprint=")]
    assert len(seeds) == 1
    return seeds[0]


@pytest.fixture
def offline_launch(monkeypatch, tmp_path):
    calls = SimpleNamespace(launches=[], starts=0, stops=0, fail=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Network or browser download attempted")

    monkeypatch.setattr(api, "ensure_binary", lambda **kw: tmp_path / "unused-binary")
    monkeypatch.setattr(api.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(api.urllib.request, "urlretrieve", forbidden)
    monkeypatch.setenv("CLOAKBROWSER_WIDEVINE", "0")

    def capture(**kwargs):
        calls.launches.append(kwargs)
        if calls.fail:
            raise RuntimeError("fixture launch failure")
        return SimpleNamespace(options=kwargs, pages=[], close=lambda: None,
                               new_context=lambda **kw: SimpleNamespace(close=lambda: None))

    def stop():
        calls.stops += 1

    def start():
        calls.starts += 1
        return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=capture, launch=capture), stop=stop)

    async def capture_async(**kwargs):
        context = capture(**kwargs)
        async def close():
            pass
        async def new_context(**kw):
            return SimpleNamespace(close=close)
        context.close, context.new_context = close, new_context
        return context

    async def stop_async():
        stop()

    async def start_async():
        calls.starts += 1
        return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=capture_async, launch=capture_async),
                               stop=stop_async)

    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(
        sync_playwright=lambda: SimpleNamespace(start=start)))
    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(
        async_playwright=lambda: SimpleNamespace(start=start_async)))
    return calls


def persistent(profile, asynchronous=False, **kwargs):
    if asynchronous:
        async def run():
            context = await api.launch_persistent_context_async(user_data_dir=profile, **kwargs)
            await context.close()
            return context.options["args"]
        return asyncio.run(run())
    context = api.launch_persistent_context(profile, **kwargs)
    context.close()
    return context.options["args"]


def test_persistent_sync_async_share_seed_and_preserve_args(tmp_path, monkeypatch, offline_launch):
    values = iter([101, 202, 303, 404, 505, 606])
    monkeypatch.setattr(api.secrets, "randbits", lambda bits: next(values))
    profile = tmp_path / "nested" / "profile"
    args = ["--fingerprint-platform=macos", "--window-size=800,600"]
    first = persistent(profile, args=args, timezone="UTC", locale="en-US")
    path = profile / SEED_FILE
    before = path.stat()
    second = persistent(str(profile), True)
    assert fingerprint(first) == fingerprint(second) == "101"
    assert path.read_bytes() == b"101\n"
    assert (before.st_ino, before.st_mtime_ns) == (path.stat().st_ino, path.stat().st_mtime_ns)
    assert "--fingerprint-platform=macos" in first
    assert "--fingerprint-timezone=UTC" in first and "--lang=en-US" in first
    assert len(first) == len({a.split("=", 1)[0] for a in first})
    assert args == ["--fingerprint-platform=macos", "--window-size=800,600"]
    other = persistent(tmp_path / "other")
    assert fingerprint(other) != fingerprint(first)
    assert list(profile.iterdir()) == [path]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert offline_launch.starts == offline_launch.stops == 3


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("value", ["42", "off", "0", ""])
def test_explicit_fingerprint_bypasses_profile_io(tmp_path, offline_launch, asynchronous, value):
    profile = tmp_path / "profile"
    args = ["--fingerprint=13", f"--fingerprint={value}"]
    assert fingerprint(persistent(profile, asynchronous, args=args)) == value
    assert not profile.exists()
    profile.mkdir()
    path = profile / SEED_FILE
    path.write_bytes(b"corrupt")
    assert fingerprint(persistent(profile, asynchronous, args=args)) == value
    assert path.read_bytes() == b"corrupt"
    assert args == ["--fingerprint=13", f"--fingerprint={value}"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_stealth_false_bypasses_profile_io(tmp_path, offline_launch, asynchronous):
    profile = tmp_path / "profile"
    args = persistent(profile, asynchronous, stealth_args=False)
    assert not any(a.startswith("--fingerprint") for a in args)
    assert not profile.exists()
    profile.mkdir()
    path = profile / SEED_FILE
    path.write_bytes(b"corrupt")
    persistent(profile, asynchronous, stealth_args=False)
    assert fingerprint(persistent(profile, asynchronous, stealth_args=False, args=["--fingerprint=42"])) == "42"
    assert path.read_bytes() == b"corrupt"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_override_does_not_replace_saved_seed(tmp_path, offline_launch, asynchronous):
    profile = tmp_path / "profile"
    original = fingerprint(persistent(profile, asynchronous))
    assert fingerprint(persistent(profile, asynchronous, args=["--fingerprint=42"])) == "42"
    assert fingerprint(persistent(profile, asynchronous, args=["--fingerprint=off"])) == "off"
    assert fingerprint(persistent(profile, asynchronous)) == original
    assert (profile / SEED_FILE).read_bytes() == f"{original}\n".encode()


@pytest.mark.parametrize("data", [b"", b"garbage", b"0\n", b"-1\n", b"4294967296\n", b"1.0\n",
                                 b"01\n", b" 1\n", b"1", b"1\r\n", b"1\n2\n", b"\xff\n"])
def test_invalid_seed_fails_without_identity_rotation(tmp_path, offline_launch, data):
    path = tmp_path / SEED_FILE
    path.write_bytes(data)
    for asynchronous in (False, True):
        with pytest.raises(ValueError, match="Invalid Chromix profile seed file"):
            persistent(tmp_path, asynchronous)
    assert path.read_bytes() == data
    assert list(tmp_path.iterdir()) == [path]
    assert offline_launch.starts == 0


@pytest.mark.parametrize("seed", [1, 4294967295])
def test_existing_seed_boundaries(tmp_path, offline_launch, seed):
    (tmp_path / SEED_FILE).write_bytes(f"{seed}\n".encode())
    assert fingerprint(persistent(tmp_path)) == fingerprint(persistent(tmp_path, True)) == str(seed)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("profile", [None, ""])
def test_persistent_requires_profile(offline_launch, asynchronous, profile):
    with pytest.raises(ValueError, match="requires user_data_dir"):
        persistent(profile, asynchronous)
    assert offline_launch.starts == 0


@pytest.mark.parametrize("asynchronous", [False, True])
def test_failed_launch_retains_seed(tmp_path, offline_launch, asynchronous):
    offline_launch.fail = True
    with pytest.raises(RuntimeError, match="fixture launch failure"):
        persistent(tmp_path, asynchronous)
    seed = (tmp_path / SEED_FILE).read_text().strip()
    offline_launch.fail = False
    assert fingerprint(persistent(tmp_path, asynchronous)) == seed
    assert offline_launch.starts == offline_launch.stops == 2


@pytest.mark.parametrize("stage", ["read", "write", "link"])
def test_seed_io_errors_do_not_fall_back_to_random(tmp_path, monkeypatch, offline_launch, stage):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "fixture denied")
    if stage == "read":
        monkeypatch.setattr(Path, "read_bytes", denied)
    elif stage == "write":
        monkeypatch.setattr(api.os, "fsync", denied)
    else:
        monkeypatch.setattr(api.os, "link", denied)
    for asynchronous in (False, True):
        with pytest.raises(PermissionError):
            persistent(tmp_path, asynchronous)
    assert list(tmp_path.iterdir()) == []
    assert offline_launch.starts == 0


def test_concurrent_seed_publication_is_complete(tmp_path, monkeypatch):
    barrier = threading.Barrier(12)
    original_link = api.os.link

    def publish(source, target):
        data = Path(source).read_bytes()
        assert data == f"{int(data)}\n".encode()
        barrier.wait(timeout=15)
        original_link(source, target)

    monkeypatch.setattr(api.os, "link", publish)
    with ThreadPoolExecutor(max_workers=12) as workers:
        seeds = list(workers.map(api._profile_seed, [tmp_path] * 12))
    assert len(set(seeds)) == 1
    assert (tmp_path / SEED_FILE).read_bytes() == f"{seeds[0]}\n".encode()
    assert list(tmp_path.iterdir()) == [tmp_path / SEED_FILE]


def test_orphan_temporary_file_does_not_block_initialization(tmp_path, offline_launch):
    orphan = tmp_path / f"{SEED_FILE}.interrupted"
    orphan.write_bytes(b"12")
    seed = fingerprint(persistent(tmp_path))
    assert (tmp_path / SEED_FILE).read_bytes() == f"{seed}\n".encode()
    assert orphan.read_bytes() == b"12"


def test_nonpersistent_launches_keep_per_launch_randomness(tmp_path, monkeypatch, offline_launch):
    values = iter(range(1, 9))
    monkeypatch.setattr(api.secrets, "randbits", lambda bits: next(values))
    for launch in (api.launch, api.launch_context):
        launch().close()
        launch().close()
    async def run():
        for launch in (api.launch_async, api.launch_context_async):
            for _ in range(2):
                obj = await launch()
                await obj.close()
    asyncio.run(run())
    assert [fingerprint(call["args"]) for call in offline_launch.launches] == [str(i) for i in range(1, 9)]
    assert list(tmp_path.iterdir()) == []
