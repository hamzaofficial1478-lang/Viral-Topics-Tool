"""M8 — Branding (Phase 2: logo overlay).

Resolves the operator's logo config into a spec the render filtergraph consumes:
corner (TL/TR/BL/BR), size (fraction of output width or explicit px), opacity.
Intro/outro cards and lower-thirds are left for a later iteration.
"""

from __future__ import annotations

import os
from typing import Any

from ..config import Config
from ..render.compose import logo_position
from ..utils import log


def logo_spec(cfg: Config, out_w: int, out_h: int) -> dict[str, Any] | None:
    """Return an overlay spec dict, or None if no (valid) logo is configured."""
    path = cfg.get("brand.logo")
    if not path:
        return None
    if not os.path.isfile(path):
        log.warning("brand.logo not found, skipping overlay: %s", path)
        return None

    size = cfg.get("brand.size", 0.18)  # fraction of width, or px if >1
    width = int(size * out_w) if 0 < float(size) <= 1 else int(size)
    width = max(24, min(width, out_w))
    opacity = float(cfg.get("brand.opacity", 0.85))
    margin = int(cfg.get("brand.margin", 40))
    corner = cfg.get("brand.corner", "TR")
    x, y = logo_position(corner, out_w, out_h, margin)
    return {"path": os.path.abspath(path), "x": x, "y": y, "width": width, "alpha": opacity}
