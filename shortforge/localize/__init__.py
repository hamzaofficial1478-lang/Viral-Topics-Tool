"""M6 — Localization: translation + dubbing (Phase 3)."""

from .translate import translate_segments, LANG_NAMES
from .dub import build_translated_transcript, dub_clip

__all__ = [
    "translate_segments",
    "build_translated_transcript",
    "dub_clip",
    "LANG_NAMES",
]
