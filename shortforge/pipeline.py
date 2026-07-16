"""In-process orchestrator (Phase 1).

Runs the state machine end to end on one source video. Phase 1 deliberately
stays a plain in-process pipeline — the Celery/Redis job queue is Phase 4.

    ingest -> transcribe -> detect -> select -> (reframe + captions + render)*
    -> manifest.json
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re

from .analyze import load_external_transcript, transcribe
from .cache import Cache
from .captions import build_ass, subtitles_filter
from .config import Config
from .detect import detect_hooks
from .ingest import ingest
from .models import Clip
from .reframe import build_filtergraph, parse_aspect
from .render import render_clip
from .select import build_clips, recommend_clip_count
from .utils import ShortForgeError, ffprobe_info, log, require_binary


def _slug(text: str, fallback: str = "clip") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or fallback


def _find_fontsdir() -> str | None:
    for d in ("/usr/share/fonts", "/Library/Fonts", "/System/Library/Fonts"):
        if os.path.isdir(d):
            return d
    return None


def run_pipeline(
    source: str,
    cfg: Config,
    *,
    owner_confirmed: bool,
    transcript_path: str | None = None,
) -> dict:
    """Execute the full Phase 1 pipeline. Returns a manifest dict."""
    require_binary("ffmpeg")
    require_binary("ffprobe")

    work_dir = cfg.get("paths.work_dir", ".shortforge")
    out_dir = cfg.get("paths.output_dir", "out")
    os.makedirs(out_dir, exist_ok=True)

    # --- M1 ingest -------------------------------------------------------- #
    meta = ingest(source, cfg, owner_confirmed=owner_confirmed)
    cache = Cache(work_dir, meta.hash)

    # --- M2 transcribe (or use supplied transcript) ----------------------- #
    if transcript_path:
        transcript = load_external_transcript(
            transcript_path, meta.duration, meta.src_lang or "en"
        )
        cache.save_json("transcript.json", transcript.to_dict())
        log.info("using supplied transcript: %s (%d segments)",
                 transcript_path, len(transcript.segments))
    else:
        transcript = transcribe(meta, cfg, cache)

    if not transcript.segments:
        raise ShortForgeError("Transcript is empty — nothing to clip.")

    # --- M3 detect -------------------------------------------------------- #
    candidates = detect_hooks(transcript, cfg)

    # --- M4 select (with recommendation) ---------------------------------- #
    n_rec, rationale = recommend_clip_count(transcript, candidates, cfg)
    requested = int(cfg.get("select.num_clips", 0) or 0)
    log.info("recommendation: %s", rationale)
    if requested:
        log.info("operator requested %d clip%s", requested, "s" if requested != 1 else "")

    # Resolve output geometry once; keep selection + reframe in agreement.
    out_w, out_h = parse_aspect(
        cfg.get("reframe.aspect", "9:16"),
        int(cfg.get("reframe.width", 1080)),
        int(cfg.get("reframe.height", 1920)),
    )
    cfg.override("reframe.width", out_w)
    cfg.override("reframe.height", out_h)

    clips = build_clips(transcript, candidates, cfg, meta.hash)
    if not clips:
        raise ShortForgeError("No clips could be selected from this source.")
    log.info("selected %d clip%s", len(clips), "s" if len(clips) != 1 else "")

    # --- M5/M7/M13 reframe + captions + render ---------------------------- #
    probe = ffprobe_info(meta.file_path)
    fill = cfg.get("reframe.fill", "crop")
    fontsdir = _find_fontsdir()
    lang = transcript.language or "xx"
    date = _dt.date.today().strftime("%Y%m%d")
    slug = _slug(meta.title, "source")

    rendered: list[Clip] = []
    for clip in clips:
        ass_path = build_ass(
            clip, transcript, out_w, out_h, cfg, cache.path(f"clip_{clip.clip_id}.ass")
        )
        extra = subtitles_filter(ass_path, fontsdir) if ass_path else None
        fg = build_filtergraph(probe.width, probe.height, out_w, out_h, fill, extra)

        # Deterministic naming: {slug}_{lang}_{clipid}_{yyyymmdd}.mp4  (M13)
        out_path = os.path.join(out_dir, f"{slug}_{lang}_{clip.clip_id}_{date}.mp4")
        render_clip(meta.file_path, clip, fg, cfg, out_path)
        clip.file_path = os.path.abspath(out_path)
        rendered.append(clip)

    # --- manifest --------------------------------------------------------- #
    manifest = {
        "source": {
            "source": meta.source,
            "title": meta.title,
            "duration": round(meta.duration, 2),
            "language": transcript.language,
            "hash": meta.hash,
        },
        "settings": {
            "resolution": f"{out_w}x{out_h}",
            "aspect": cfg.get("reframe.aspect"),
            "fill": fill,
            "target_duration": cfg.get("select.target_duration"),
            "captions": bool(cfg.get("captions.enabled", True)),
            "hook_backend": cfg.get("detect.backend"),
        },
        "recommendation": {"recommended_clips": n_rec, "rationale": rationale},
        "clips": [c.to_dict() for c in rendered],
    }
    manifest_path = os.path.join(out_dir, f"{slug}_{date}_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    manifest["manifest_path"] = os.path.abspath(manifest_path)
    log.info("wrote manifest: %s", manifest_path)
    return manifest
