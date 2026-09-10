"""Synthetic Windows display templates passed explicitly by the SDK.

Explicit caller settings override defaults and still require layout validation.
The 85px UI strip is a template value, not a measured native window frame.
"""
from __future__ import annotations

import math
import random
from typing import Any

# Entries match GetSeededScreen's templates, not its C++ selection algorithm.
SCREEN_POOL: list[tuple[int, int, float, float]] = [
    (1920, 1080, 1.0, 0.42),
    (1366, 768, 1.0, 0.14),
    (2560, 1440, 1.0, 0.12),
    (1536, 864, 1.25, 0.11),
    (1440, 900, 1.0, 0.05),
    (1680, 1050, 1.0, 0.05),
    (1280, 720, 1.0, 0.03),
    (1600, 900, 1.0, 0.03),
    (1920, 1200, 1.0, 0.02),
    (2560, 1440, 1.5, 0.02),
    (1280, 800, 1.0, 0.01),
]

# Synthetic taskbar templates; these weights are not measured population data.
TASKBAR_POOL: list[tuple[int, float]] = [(48, 0.70), (40, 0.30)]

CHROME_UI_STRIP = 85

def _fmt_dpr(dpr: float) -> str:
    return f"{dpr:g}"


def pick_persona_screen(rand: random.Random | None = None) -> dict[str, Any]:
    """One weighted pick from the pool; returns the full derived geometry."""
    r = rand if rand is not None else random
    w, h, dpr = r.choices(
        [(s[0], s[1], s[2]) for s in SCREEN_POOL],
        weights=[s[3] for s in SCREEN_POOL])[0]
    taskbar = r.choices([t[0] for t in TASKBAR_POOL],
                        weights=[t[1] for t in TASKBAR_POOL])[0]
    return {"width": w, "height": h, "dpr": dpr, "taskbar": taskbar,
            "avail_height": h - taskbar,
            "inner_height": h - taskbar - CHROME_UI_STRIP}


class _SeededRandom(random.Random):
    def __init__(self, seed):
        self._state = (seed ^ 0x7363726E) & 0xFFFFFFFF

    def random(self):
        self._state = (self._state + 0x6D2B79F5) & 0xFFFFFFFF
        value = ((self._state ^ (self._state >> 15)) * (1 | self._state)) & 0xFFFFFFFF
        value ^= (value + ((value ^ (value >> 7)) * (61 | value))) & 0xFFFFFFFF
        return ((value ^ (value >> 14)) & 0xFFFFFFFF) / 4294967296


def ensure_persona_geometry(args: list[str] | None,
                            rand: random.Random | None = None
                            ) -> tuple[list[str], dict[str, Any]]:
    """Complete geometry; numeric seeds use the same generator as the Node SDK."""
    existing = {}
    for arg in args or []:
        key, sep, value = arg[2:].partition("=")
        if sep:
            existing[key] = value
    for dim in ("width", "height"):
        alias, key = f"fingerprint-screen-{dim}", f"uxr-screen-{dim}"
        if key not in existing and alias in existing:
            existing[key] = existing[alias]
    if rand is None:
        try:
            seed = int(existing.get("fingerprint", ""))
            if 0 < seed <= 0xFFFFFFFF:
                rand = _SeededRandom(seed)
        except ValueError:
            pass

    def number_at(key, zero=False):
        try:
            value = float(existing.get(key, ""))
            if math.isfinite(value) and (value >= 0 if zero else value > 0):
                return int(value) if value.is_integer() else value
        except ValueError:
            pass
        return None

    pick = pick_persona_screen(rand)
    w = number_at("uxr-screen-width") or pick["width"]
    h = number_at("uxr-screen-height") or pick["height"]
    dpr = number_at("uxr-device-pixel-ratio") or pick["dpr"]
    available = number_at("uxr-screen-avail-height")
    tb = number_at("uxr-taskbar-height", zero=True)
    if tb is None:
        tb = pick["taskbar"] if available is None else h - available
    try:
        window_size = [int(v) for v in existing.get("window-size", "").split(",")]
        valid_size = len(window_size) == 2 and all(v > 0 for v in window_size)
    except ValueError:
        valid_size = False
    outer_width = number_at("uxr-outer-width") or (window_size[0] if valid_size else w)
    outer_height = number_at("uxr-outer-height") or (window_size[1] if valid_size else h - tb)
    add = []

    def put(key, value):
        if key not in existing:
            add.append(f"--{key}={value}")

    put("uxr-screen-width", w)
    put("uxr-screen-height", h)
    put("uxr-device-pixel-ratio", _fmt_dpr(dpr))
    put("uxr-taskbar-height", tb)
    put("uxr-outer-width", outer_width)
    put("uxr-outer-height", outer_height)
    geometry = {"width": w, "height": h, "dpr": dpr, "taskbar": tb,
                "avail_height": h - tb, "outer_width": outer_width,
                "outer_height": outer_height,
                "inner_height": outer_height - CHROME_UI_STRIP}
    return add + list(args or []), geometry
