"""M5 — Reframe to vertical (center smart-crop + Phase-2 subject tracking)."""

from .crop import compute_crop, build_filtergraph, parse_aspect
from .resolution import apply_resolution, dims_for, estimate_export, short_side
from .track import plan_track, Track, available as tracking_available
from .vcam import plan_vcam, detect_saliency, plan_path

__all__ = [
    "compute_crop",
    "build_filtergraph",
    "parse_aspect",
    "apply_resolution",
    "dims_for",
    "estimate_export",
    "short_side",
    "plan_track",
    "plan_vcam",
    "detect_saliency",
    "plan_path",
    "Track",
    "tracking_available",
]
