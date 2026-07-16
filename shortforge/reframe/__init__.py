"""M5 — Reframe to vertical (center smart-crop + Phase-2 subject tracking)."""

from .crop import compute_crop, build_filtergraph, parse_aspect
from .track import plan_track, Track, available as tracking_available

__all__ = [
    "compute_crop",
    "build_filtergraph",
    "parse_aspect",
    "plan_track",
    "Track",
    "tracking_available",
]
