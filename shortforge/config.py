"""Configuration loading: defaults from config/settings.yaml, with overrides.

Config is kept as nested dicts and accessed with :func:`Config.get` using
dotted paths (e.g. ``cfg.get("select.target_duration")``). CLI flags override
file values via :meth:`Config.override`.
"""

from __future__ import annotations

import copy
import os
from typing import Any

import yaml

# Baked-in defaults so the pipeline runs even without a settings file present.
DEFAULTS: dict[str, Any] = {
    "paths": {"work_dir": ".shortforge", "output_dir": "out"},
    "ingest": {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "cookies": None,
    },
    "transcribe": {
        "model": "base",
        "device": "auto",
        "compute_type": "auto",
        "language": None,
    },
    "detect": {
        "backend": "auto",
        "llm_model": "claude-opus-4-8",
        "min_segment_score": 0.35,
    },
    "select": {
        "target_duration": 45,
        "tolerance": 12,
        "num_clips": 0,
        "min_gap": 1.5,
    },
    "reframe": {
        "aspect": "9:16",
        "width": 1080,
        "height": 1920,
        "fill": "crop",
        "mode": "track",            # track (follow speaker) | center
        "track_sample_interval": 0.33,
        "track_smooth_window": 5,
        "face_score": 0.6,
    },
    "captions": {
        "enabled": True,
        "style": "karaoke",         # karaoke (word highlight) | simple
        "font": "DejaVu Sans",
        "font_size": 54,
        "primary_color": "&H00FFFFFF",
        "highlight_color": "&H0000E5FF",   # amber (BGR) for the sung word
        "outline_color": "&H00000000",
        "outline": 3,
        "shadow": 1,
        "bottom_margin": 320,
        "max_line_chars": 30,
        "max_line_duration": 2.5,
    },
    "edit": {                       # M9 silence trim (planning built + tested)
        "jumpcuts": False,
        "min_silence": 0.8,
        "max_gap": 0.35,
        "pad": 0.1,
    },
    "brand": {                      # M8 logo overlay
        "logo": None,
        "corner": "TR",             # TL | TR | BL | BR
        "size": 0.18,               # fraction of width (<=1) or px (>1)
        "opacity": 0.85,
        "margin": 40,
    },
    "metadata": {"enabled": True, "backend": "auto"},   # M10
    "thumbnail": {"enabled": True, "text_hook": False},  # M11
    "page": {"niche": None, "tone": None, "hashtags": []},  # Section 8 profile
    "render": {
        "crf": 20,
        "preset": "veryfast",
        "audio_bitrate": "128k",
        "fps": None,
        "loudnorm": True,           # M9: normalise to ~-14 LUFS
        "loudnorm_i": -14.0,
        "loudnorm_tp": -1.5,
        "loudnorm_lra": 11.0,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        data = copy.deepcopy(DEFAULTS)
        candidates = [path] if path else [
            os.path.join("config", "settings.yaml"),
            os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml"),
        ]
        for cand in candidates:
            if cand and os.path.isfile(cand):
                with open(cand, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                data = _deep_merge(data, loaded)
                break
        return cls(data)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def override(self, dotted: str, value: Any) -> None:
        """Set a value by dotted path (used by CLI flags). None is ignored."""
        if value is None:
            return
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def section(self, name: str) -> dict[str, Any]:
        return dict(self._data.get(name, {}))

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)
