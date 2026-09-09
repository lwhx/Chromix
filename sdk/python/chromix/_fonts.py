"""Linux Fontconfig wiring for the bundled Windows font assets."""
from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

_FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}

# Same shape as assets/fonts/fonts.conf.template: expose exactly one font
# directory plus a private cache. Used when the caller supplies their own
# font directory (which then replaces the bundled directory).
_FONTS_CONF_TEMPLATE = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>@FONTS_DIR@</dir>
  <cachedir>@CACHE_DIR@</cachedir>
</fontconfig>
"""


def _decode_name(raw: bytes, platform_id: int) -> str:
    try:
        if platform_id in (0, 3):  # Unicode / Windows -> UTF-16BE
            return raw.decode("utf-16-be", errors="ignore").replace("\x00", "").strip()
        if platform_id == 1:  # Macintosh -> Mac Roman
            return raw.decode("mac_roman", errors="ignore").replace("\x00", "").strip()
    except (LookupError, ValueError):
        pass
    return ""


def _sfnt_family_names(fh, base: int) -> set[str]:
    """Family names (name IDs 1 and 16) of one sfnt at file offset ``base``.

    Reads only the table directory and the ``name`` table, never whole files.
    """
    try:
        fh.seek(base + 4)
        (num_tables,) = struct.unpack(">H", fh.read(2))
        fh.seek(base + 12)  # table directory starts after the 12-byte sfnt header
        name_off = None
        for _ in range(min(num_tables, 256)):
            rec = fh.read(16)
            if len(rec) < 16:
                return set()
            tag, _sum, off, _length = struct.unpack(">4sIII", rec)
            if tag == b"name":
                name_off = off
                break
        if name_off is None:
            return set()
        fh.seek(name_off)
        _fmt, count, str_off = struct.unpack(">HHH", fh.read(6))
        records = []
        for _ in range(min(count, 4096)):
            rec = fh.read(12)
            if len(rec) < 12:
                return set()
            records.append(struct.unpack(">HHHHHH", rec))
        families: set[str] = set()
        for platform_id, _enc, _lang, name_id, length, soff in records:
            if name_id not in (1, 16) or length == 0 or length > 1024:
                continue
            fh.seek(name_off + str_off + soff)
            text = _decode_name(fh.read(length), platform_id)
            if text:
                families.add(text)
        return families
    except (OSError, struct.error):
        return set()


def _file_family_names(path: Path) -> set[str]:
    try:
        with path.open("rb") as fh:
            magic = fh.read(4)
            if magic == b"ttcf":  # TrueType Collection: several sfnts
                fh.seek(8)
                (num_fonts,) = struct.unpack(">I", fh.read(4))
                # Read all member offsets up front — parsing seeks the cursor.
                offsets = []
                for _ in range(min(num_fonts, 64)):
                    raw = fh.read(4)
                    if len(raw) < 4:
                        break
                    (off,) = struct.unpack(">I", raw)
                    offsets.append(off)
                names: set[str] = set()
                for off in offsets:
                    names |= _sfnt_family_names(fh, off)
                return names
            return _sfnt_family_names(fh, 0)
    except (OSError, struct.error):
        return set()


def font_families_in_dir(fonts_dir: str | os.PathLike) -> list[str]:
    """Ordered, de-duplicated family names across all fonts in ``fonts_dir``.

    Parses OpenType name tables (IDs 1 and 16) of .ttf/.otf/.ttc files —
    the same data Fontconfig and DirectWrite resolve families from. Legacy
    .fon bitmap fonts carry no name table and are skipped.
    """
    names: list[str] = []
    seen: set[str] = set()
    try:
        # Case-insensitive file order keeps the generated whitelist stable
        # across platforms (Windows Path ordering is case-insensitive,
        # POSIX is not).
        entries = sorted(Path(fonts_dir).iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return names
    for path in entries:
        if not path.is_file() or path.suffix.lower() not in _FONT_SUFFIXES:
            continue
        for name in sorted(_file_family_names(path)):
            if name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
    return names


def font_dir_whitelist_arg(fonts_dir: str | os.PathLike) -> str | None:
    """``--uxr-font-whitelist`` covering every family found in ``fonts_dir``.

    Returns None when the directory has no parseable fonts (caller should
    then leave the engine default untouched).
    """
    families = font_families_in_dir(fonts_dir)
    if not families:
        return None
    return "--uxr-font-whitelist=" + ",".join(families)


def _write_fontconfig(fonts_dir: Path, template: str) -> str | None:
    try:
        cache_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) \
            / "chromix" / "fontconfig"
        cache_dir.mkdir(parents=True, exist_ok=True)
        config = template.replace("@FONTS_DIR@", str(fonts_dir))
        config = config.replace("@CACHE_DIR@", str(cache_dir))
        config_path = Path(tempfile.gettempdir()) / f"chromix-fontconfig-{os.getuid()}.conf"
        config_path.write_text(config, encoding="utf-8")
        return str(config_path)
    except OSError:
        return None


def linux_font_env(executable: str | os.PathLike,
                   fonts_dir: str | os.PathLike | None = None) -> dict[str, str]:
    """Return ``FONTCONFIG_FILE`` for the given (or bundled) font directory.

    ``fonts_dir`` replaces the bundled ``fonts/`` next to the executable;
    without it, a Linux bundle containing ``fonts/`` is wired as before.
    """
    if sys.platform != "linux":
        return {}
    if fonts_dir is not None:
        config = _write_fontconfig(Path(fonts_dir).resolve(), _FONTS_CONF_TEMPLATE)
        return {"FONTCONFIG_FILE": config} if config else {}

    bundled_dir = Path(executable).resolve().parent / "fonts"
    template = bundled_dir / "fonts.conf.template"
    if not template.is_file():
        return {}
    try:
        config = _write_fontconfig(bundled_dir, template.read_text(encoding="utf-8"))
        return {"FONTCONFIG_FILE": config} if config else {}
    except OSError:
        return {}


def apply_font_env(executable: str | os.PathLike,
                   launch_kwargs: dict[str, Any],
                   fonts_dir: str | os.PathLike | None = None) -> None:
    """Merge the font environment into Playwright launch options.

    Caller-provided ``env`` entries always win over the generated ones.
    """
    font_env = linux_font_env(executable, fonts_dir)
    user_env = launch_kwargs.get("env")
    if not font_env and user_env is None:
        return
    merged = dict(os.environ)
    merged.update(font_env)
    if user_env:
        merged.update(user_env)
    launch_kwargs["env"] = merged
