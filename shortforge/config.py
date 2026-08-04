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
        "cookies": None,                 # path to a cookies.txt (Netscape format)
        "cookies_from_browser": None,    # firefox | chrome | edge | brave (read the browser's cookies)
        "socket_timeout": 120,           # per-read timeout (s); 20 was too short on slow links
        "retries": 4,                    # network retries per auth strategy (exponential backoff)
    },
    "transcribe": {
        "model": "small",           # tiny|base|small|medium|large-v3 ('base' garbles; 'small' is the sane CPU default)
        "device": "auto",
        "compute_type": "auto",
        "language": None,           # None = autodetect; ISO code forces the source language
        "min_confidence": 0.0,      # >0 excludes low-confidence segments from clip selection
    },
    "detect": {
        "backend": "auto",          # auto | provider (C1 LLM+frames) | llm | heuristic
        "llm_model": "claude-opus-4-8",
        "min_segment_score": 0.35,
        # C1: provider hook scorer (task-routing LLM + vision frame fusion).
        # Vision is OFF by default: transcript-only already picks good clips, and
        # llama-3.2-11b-vision takes ONE image per request (6 frames = 6 calls per
        # candidate) so it's a slow bonus, not a blocker. Turn on with --hook-frames.
        "hook_frames": False,       # fuse vision frame scores onto the top candidates
        "frame_weight": 0.35,       # (1-w)*transcript_llm + w*frames_llm
        "frame_topk": 12,           # only score frames for the top-K candidates (cost)
        "hook_batch": 12,           # segments per hook-scoring call (small+predictable beats fewer+large; timeouts split to half)
        "hook_timeout": 0,          # per-call timeout override (s); 0 = task default (600)
        "hook_concurrency": 4,      # hook-scoring batches run in parallel (respect provider RPM)
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
        "max_backup": 2,          # max segments to back up from a hook anchor (keep hook early)
    },
    "reframe": {
        "aspect": "9:16",           # SHAPE (9:16 / 1:1 / 16:9 / WxH)
        "resolution": "1080p",      # SIZE — short side px (1080p/720p/480p) or WxH; separate from aspect
        "width": 1080,
        "height": 1920,
        "fill": "crop",
        "mode": "track",            # track (virtual camera) | center | static
        "track_sample_interval": 0.33,   # legacy tracker
        "track_smooth_window": 5,        # legacy tracker
        "face_score": 0.6,
        # A4 virtual camera:
        "sample_hz": 4,             # saliency sample rate (interpolated + smoothed; 6 over-samples)
        "redetect_on_scene_cut": True,
        "scene_cut_threshold": 0.18,  # mean frame-diff fraction that marks a cut
        "motion_min_area_pct": 0.8,   # min changed-area % to treat motion as salient (was 0.3: too twitchy)
        # Camera smoothness. These defaults favour STEADY footage over chasing
        # every movement — a shaky auto-crop looks worse than a slightly late one.
        "deadzone_pct": 12,         # ignore camera moves under this % of width
        "max_pan_speed_pct_s": 22,  # cap pan speed (% of width / second) — gentler sweep
        "min_dwell_s": 2.0,         # min time before honoring a new target
        "snap_threshold_pct": 60,   # displacement over this % => hard cut (else pan smoothly)
        "face_switch_ratio": 1.4,   # a different face must be this much bigger to steal the camera
        "smoothing": "one_euro",    # documented; planner eases + caps velocity
        "multi_subject": "cut",     # cut (follow speaker) | split | widen
        "punch_in_max_scale": 1.12,
        "punch_in_min_gap_s": 6,
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
        # A3 burned-in source-caption handling:
        "burned_in": "none",        # none | cover | blur | crop
        "burned_in_samples": 16,
        "burned_in_threshold": 0.10,
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
    # STEP 9: title/description/tags + thumbnail are OPT-IN (off by default). The
    # pipeline focuses on finding + rendering clips; these secondary "Extras" cost
    # an LLM call per clip, so they run only when explicitly enabled (--metadata /
    # --thumbnail, or the UI Extras toggles). When off, the stage never runs — no
    # provider resolved, no call, no placeholder file.
    "metadata": {"enabled": False, "backend": "auto"},   # M10 (opt-in)
    "publish": {"sidecar": True},                        # M14 sidecar (only if metadata on)
    "thumbnail": {"enabled": False, "text_hook": False},  # M11 (opt-in)
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
        "stem_model": "htdemucs",    # Demucs model (better: htdemucs_ft; lighter: mdx_extra_q)
        "demucs_model": "htdemucs",  # back-compat alias
        # G1: ambience Demucs mis-routes into the vocals stem is otherwise lost.
        "vocal_removal_strength": "partial",  # partial | full | <dB e.g. -18>
        "vocal_retain_db": -18.0,    # level to keep the vocals stem at (partial)
        "debug_audio": False,        # --debug-audio: export stems + bed WAVs
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
    "cache": {                       # A2 cache controls
        "disabled": False,           # --no-cache: never read or write caches
        "refresh_translation": False,  # --refresh-translation: recompute translation only
    },
    "cost": {                        # STEP 1 cost controls (paid TTS)
        "max_usd_per_job": 0.0,      # per-job ceiling; 0 = no limit. Abort if exceeded.
        "confirm": True,             # prompt for confirmation when estimate > $0 (CLI)
        "dry_run": False,            # --dry-run-cost: estimate only, no synthesis/render
    },
    "review": {"enabled": True},     # M12 QC review gate
    "render": {
        "crf": 20,
        "preset": "veryfast",
        "audio_bitrate": "128k",
        "fps": None,
        "resume": False,            # skip clips whose output already exists
        # STEP 5 parallel render: run clips concurrently (each ffmpeg is CPU-bound).
        "workers": 0,               # 0 = auto min(clips, physical_cores//2); 1 = sequential
        "threads": 0,               # per-ffmpeg -threads; 0 = auto (logical_cores // workers)
        # STEP 1 hardware encoding. Default x264 (software) until the operator
        # confirms QSV quality via `encode-sample`; then set 'auto' or 'qsv'.
        "encoder": "x264",          # auto | qsv | nvenc | amf | x264
        "qsv_quality": 23,          # h264_qsv -global_quality (CRF-like; lower = better)
        "nvenc_cq": 23,             # h264_nvenc -cq
        "amf_qp": 22,               # h264_amf -qp_i/-qp_p

        "loudnorm": True,           # M9: normalise to ~-14 LUFS
        "loudnorm_i": -14.0,
        "loudnorm_tp": -1.5,
        "loudnorm_lra": 11.0,
    },
    "vision": {                     # R7: frame scoring adapter
        "max_images": 1,            # llama-3.2-11b-vision: "At most 1 image may be provided" (confirmed live)
        "frame_width": 768,         # downscale cap
        "frames_per_clip": 2,       # frames sampled per candidate window (1 img/call ⇒ N calls; keep low)
        "timeout": 0,               # per-call timeout override (s); 0 = task default (300)
    },
    "providers": {
        "retries": 2,               # retries on a *timeout*, with exponential backoff (2s, 4s)
        "request_timeout": 120,     # global default per-call timeout (s); tasks/credentials override
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
