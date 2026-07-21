"""Export resolution (pixel size) — deliberately SEPARATE from aspect (shape).

Aspect (9:16 / 1:1 / 16:9 / WxH) sets the *shape*; resolution sets the *short
side* in pixels (1080p → 1080, 720p → 720, 480p → 480). A custom ``WxH`` in the
resolution field overrides the aspect with exact dimensions.
"""

from __future__ import annotations

import re

_PRESETS = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}


def short_side(resolution: str) -> int | None:
    r = str(resolution or "1080p").lower().strip()
    if r in _PRESETS:
        return _PRESETS[r]
    m = re.match(r"^(\d+)p$", r)
    return int(m.group(1)) if m else None


def apply_resolution(cfg) -> None:
    """Set ``reframe.width``/``reframe.height`` (or ``reframe.aspect`` for a custom
    WxH) from ``reframe.resolution`` — so the existing parse_aspect flow anchors on
    the chosen short side."""
    r = str(cfg.get("reframe.resolution", "1080p")).lower().strip()
    if "x" in r:                              # exact WxH overrides aspect
        cfg.override("reframe.aspect", r)
        return
    s = short_side(r) or 1080
    cfg.override("reframe.width", s)
    cfg.override("reframe.height", s)


def dims_for(resolution: str, aspect: str) -> tuple[int, int]:
    from .crop import parse_aspect
    if "x" in str(resolution).lower():
        return parse_aspect(str(resolution), 1080, 1920)
    s = short_side(resolution) or 1080
    return parse_aspect(aspect or "9:16", s, s)


def estimate_export(resolution: str, aspect: str, duration_s: float, n_clips: int = 1) -> dict:
    """Rough export size (MB) + relative CPU render speed, for the wizard/UI.

    Heuristic only — a 1080×1920 clip at CRF 20 is ~6 Mbps; bitrate and CPU render
    time both scale roughly with pixel count. Lower resolutions render meaningfully
    faster on CPU, which is why this is worth surfacing.
    """
    w, h = dims_for(resolution, aspect)
    pixels = max(1, w * h)
    ref = 1080 * 1920
    mbps = max(0.8, 6.0 * pixels / ref)
    mb_per_clip = mbps * max(0.0, duration_s) / 8.0
    rel = pixels / ref
    if 0.9 <= rel <= 1.1:
        speed = "baseline"
    elif rel < 1:
        speed = f"~{1 / rel:.1f}× faster than 1080p"
    else:
        speed = f"~{rel:.1f}× slower than 1080p"
    return {"width": w, "height": h, "mb_per_clip": round(mb_per_clip, 1),
            "total_mb": round(mb_per_clip * max(1, n_clips), 1), "render_speed": speed}
