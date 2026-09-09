"""Persona screen geometry: one coherent pick drives every surface.

The engine (patch 0018/0012) falls back to its own seed-pooled screen when no
explicit --uxr-screen-* switches are present. A fixed SDK viewport on top of a
randomly pooled screen is an instant tell (innerWidth > screen.width), so the
SDK picks the geometry itself and passes every piece explicitly:

    screen.width/height  = pick (w, h)            --uxr-screen-width/height
    screen.availHeight   = h - taskbar            --uxr-taskbar-height
    window.outerWidth    = w                      --uxr-outer-width
    window.outerHeight   = h - taskbar            --uxr-outer-height
    window.innerWidth    = w          (headless: CDP viewport)
    window.innerHeight   = h - taskbar - 85       (headless: CDP viewport)
    devicePixelRatio     = pick (dpr, joint)      --uxr-device-pixel-ratio

85px is the Win11 Chrome UI strip (title + tabs + omnibox), same constant the
engine uses for its outerHeight default in patch 0012.
"""
from __future__ import annotations

import random
from typing import Any

# Mirrors the engine pool in patch 0002 (base/uxr_config.cc GetSeededScreen):
# joint (logical size, dpr) picks so width * dpr lands on a real panel.
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

# Win11 taskbar 48px (~70%) / Win10 40px (~30%), mirrors GetSeededTaskbarHeight.
TASKBAR_POOL: list[tuple[int, float]] = [(48, 0.70), (40, 0.30)]

CHROME_UI_STRIP = 85

_GEOMETRY_KEYS = ("uxr-screen-width", "uxr-screen-height",
                  "uxr-device-pixel-ratio", "uxr-taskbar-height")


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


def ensure_persona_geometry(args: list[str] | None,
                            rand: random.Random | None = None
                            ) -> tuple[list[str], dict[str, Any]]:
    """Complete the screen persona in ``args``; returns (args, geometry).

    Any geometry piece the caller already set via --uxr-* wins; missing pieces
    come from a single pooled pick so screen/avail/outer/inner always agree.
    Idempotent: running on already-completed args adds nothing.
    """
    existing: dict[str, str] = {}
    for a in (args or []):
        if a.startswith("--uxr-"):
            key, sep, val = a[len("--uxr-"):].partition("=")
            if sep:
                existing["uxr-" + key] = val

    def _int(key: str) -> int:
        try:
            v = int(existing.get(key, ""))
            return v if v > 0 else 0
        except ValueError:
            return 0

    def _float(key: str) -> float:
        try:
            v = float(existing.get(key, ""))
            return v if v > 0.0 else 0.0
        except ValueError:
            return 0.0

    w, h = _int("uxr-screen-width"), _int("uxr-screen-height")
    dpr, tb = _float("uxr-device-pixel-ratio"), _int("uxr-taskbar-height")
    if not (w and h and dpr and tb):
        pick = pick_persona_screen(rand)
        w = w or pick["width"]
        h = h or pick["height"]
        dpr = dpr or pick["dpr"]
        tb = tb or pick["taskbar"]

    add: list[str] = []

    def _put(key: str, val: str) -> None:
        if key not in existing:
            add.append(f"--{key}={val}")

    _put("uxr-screen-width", str(w))
    _put("uxr-screen-height", str(h))
    _put("uxr-device-pixel-ratio", _fmt_dpr(dpr))
    _put("uxr-taskbar-height", str(tb))
    _put("uxr-outer-width", str(w))
    _put("uxr-outer-height", str(h - tb))

    geometry = {"width": w, "height": h, "dpr": dpr, "taskbar": tb,
                "avail_height": h - tb, "inner_height": h - tb - CHROME_UI_STRIP}
    # Persona switches go first so explicit user args keep priority in
    # build_args' key dedupe.
    return add + list(args or []), geometry
