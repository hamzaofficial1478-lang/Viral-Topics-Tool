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
        # M3++ visual hook signals (local, no API).
        "visual": True,
        "visual_weight": 0.35,          # blend: (1-w)*transcript + w*visual
        "visual_sample_interval": 0.5,  # seconds between sampled frames
        "visual_max_samples": 1500,
        "visual_cut_threshold": 0.25,   # frame-diff level counted as a scene cut
        "visual_motion_ref": 0.12,      # motion level that saturates the score
        "visual_cut_ref": 0.5,          # cuts/sec that saturates the score
        "visual_face_ref": 0.12,        # face frame-fraction that saturates
        "visual_w_motion": 0.4,
        "visual_w_cuts": 0.3,
        "visual_w_face": 0.3,
        # Claude-vision multimodal scorer (needs API key + vision) — claude-watch style.
        "vision_llm": False,
        "vision_hook_secs": 15,      # dense-sampled hook window
        "vision_hook_fps": 6,        # hook sampling rate
        "vision_body_interval": 3.5, # seconds/frame over the body
        "vision_max_sheets": 6,      # cap contact sheets sent to the API
    },
    "select": {
        "target_duration": 45,
        "tolerance": 12,
        "num_clips": 0,
        "min_gap": 1.5,
        # Coherent-story selection: start/end clips on natural thought boundaries
        # (pauses / topic shifts) so each short is a complete idea, not a window.
        "coherent": True,
        "pause_threshold": 0.5,   # gap (s) that marks a thought/topic boundary
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
        "template": "clean",        # clean|bold_pop|karaoke_amber|reveal_green|boxed|minimal
        "animation": None,          # None = template default; none|fade|pop|karaoke|reveal
        "style": None,              # legacy: karaoke|simple (maps to animation)
        # The look defaults to the template; set any of these to override it.
        "font": None,
        "font_size": None,
        "uppercase": None,
        "primary_color": None,
        "highlight_color": None,
        "outline_color": None,
        "outline": None,
        "shadow": None,
        "bottom_margin": None,
        # Line-grouping (behaviour, not look):
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
    "publish": {"sidecar": True},                        # M14 title/desc/tags sidecar
    "thumbnail": {"enabled": True, "text_hook": False},  # M11
    "page": {"niche": None, "tone": None, "hashtags": []},  # Section 8 profile
    "localize": {                    # M6 localization (Phase 3)
        "language": None,            # target lang code (None = keep source)
        "dub": False,                # synthesize voiceover (else captions-only)
        "tts_backend": "auto",       # auto | espeak | edge | xtts
        "espeak_speed": 165,
        "edge_voice": None,
        "voice_sample": None,        # reference clip for xtts voice cloning
        "translate_backend": "auto", # auto | llm | argos
        "allow_untranslated": False, # A2: proceed on failed translation (loud warn)
        # A1 stem separation (remove original voice, keep music/SFX):
        "stem_separation": "auto",   # auto | true | false
        "stem_model": "htdemucs",    # Demucs model (lighter: mdx_extra_q)
        "demucs_model": "htdemucs",  # back-compat alias
        "accompaniment_gain_db": 0.0,  # music/SFX bed level
        "duck_db": -6.0,             # sidechain duck depth under the dub voice
        "duck_attack_ms": 5,
        "duck_release_ms": 250,
        "allow_voice_bleed": False,  # A1: allow ducking the ORIGINAL (two voices)
        "bleed_duck_db": -12.0,      # duck depth for the --allow-voice-bleed path
    },
    "lipsync": {                     # Optional Wav2Lip lip-sync (cross-language dubs only)
        "enabled": False,            # off by default — needs a GPU + Wav2Lip checkout
        "backend": "auto",           # auto | wav2lip
        "wav2lip_repo": None,        # path to a cloned Rudrabha/Wav2Lip (has inference.py)
        "checkpoint": None,          # path to wav2lip_gan.pth
        "python": None,              # interpreter for Wav2Lip's env (None = current)
        "pads": "0 10 0 0",          # face padding top bottom left right
        "resize_factor": 1,          # downscale before inference for speed
        "nosmooth": False,           # disable temporal mouth smoothing
    },
    "review": {"enabled": True},     # M12 QC review gate
    "render": {
        "crf": 20,
        "preset": "veryfast",
        "audio_bitrate": "128k",
        "fps": None,
        "resume": False,            # skip clips whose output already exists

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
