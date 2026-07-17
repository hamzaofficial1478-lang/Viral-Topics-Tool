"""In-process orchestrator (Phases 1-3).

Runs the state machine end to end on one source video. It deliberately stays a
plain in-process pipeline — the Celery/Redis job queue is Phase 4.

    ingest -> transcribe -> detect -> select -> [translate + dub] ->
    (reframe + captions + brand + render + metadata + thumbnail + QC)* -> manifest.json
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re

from .analyze import load_external_transcript, transcribe
from .analyze.audio import plan_keep_ranges, total_kept
from .brand import logo_spec
from .cache import Cache
from .captions import build_ass, subtitles_filter
from .captions.templates import resolve as resolve_caption_style
from .config import Config
from .detect import detect_hooks
from .ingest import ingest
from .lipsync import available as lipsync_available, lipsync_clip
from .localize import build_translated_transcript, dub_clip
from .metadata import generate as gen_metadata
from .models import Clip
from .publish import write_sidecar
from .qc import review_clip
from .reframe import parse_aspect, plan_track, tracking_available
from .render import build_filtergraph, render_clip, render_clip_tracked
from .select import build_clips, recommend_clip_count
from .thumbnail import make_thumbnail
from .utils import ShortForgeError, ffprobe_info, log, require_binary

_RTL_LANGS = {"ar", "he", "fa", "ur"}


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

    # --- M3 detect (transcript + visual signals) -------------------------- #
    candidates = detect_hooks(transcript, cfg, meta.file_path)

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

    # --- M5/M7/M8/M13 reframe + captions + branding + render -------------- #
    probe = ffprobe_info(meta.file_path)
    cfg.override("reframe._src_w", probe.width)
    cfg.override("reframe._src_h", probe.height)
    fill = cfg.get("reframe.fill", "crop")
    mode = cfg.get("reframe.mode", "track")
    fontsdir = _find_fontsdir()
    lang = transcript.language or "xx"
    date = _dt.date.today().strftime("%Y%m%d")
    slug = _slug(meta.title, "source")

    logo = logo_spec(cfg, out_w, out_h)
    _cap_style = resolve_caption_style(cfg) if cfg.get("captions.enabled", True) else None
    if _cap_style:
        log.info("caption template: %s (%s animation)",
                 _cap_style["_template"], _cap_style["animation"])
    do_meta = bool(cfg.get("metadata.enabled", True))
    do_thumb = bool(cfg.get("thumbnail.enabled", True))
    do_review = bool(cfg.get("review.enabled", True))
    do_publish = bool(cfg.get("publish.sidecar", True))
    resume = bool(cfg.get("render.resume", False))

    # --- M6 localization: target-language captions + optional dub ---------- #
    target_lang = cfg.get("localize.language")
    src_lang = transcript.language or "en"
    localize_on = bool(target_lang) and str(target_lang).split("-")[0] != src_lang
    dub_on = localize_on and bool(cfg.get("localize.dub", False))
    # Lip-sync only makes sense over a NEW voiceover (cross-language dub); it is
    # never applied to same-language clips (real lips already match).
    lipsync_on = dub_on and bool(cfg.get("lipsync.enabled", False))
    if lipsync_on:
        ok, reason = lipsync_available(cfg)
        if ok:
            log.info("lip-sync enabled (Wav2Lip): dubbed clips will be mouth-synced")
        else:
            log.info("lip-sync requested but unavailable (%s); clips render normally", reason)
            lipsync_on = False
    if localize_on:
        cache_key = f"transcript_{target_lang}.json"
        cached_tr = cache.load_json(cache_key)
        if cached_tr:
            from .models import Transcript
            caption_transcript = Transcript.from_dict(cached_tr)
            log.info("using cached %s translation", target_lang)
        else:
            log.info("translating captions %s -> %s", src_lang, target_lang)
            caption_transcript = build_translated_transcript(transcript, target_lang, cfg)
            cache.save_json(cache_key, caption_transcript.to_dict())
        clip_lang = target_lang
        # RTL scripts: karaoke/reveal per-word tags can mis-order; use fade.
        if target_lang.split("-")[0] in _RTL_LANGS and _cap_style and \
                _cap_style["animation"] in ("karaoke", "reveal"):
            log.info("RTL language %s: switching caption animation to fade", target_lang)
            cfg.override("captions.animation", "fade")
        if dub_on:
            log.info("dubbing voiceover in %s (tts=%s)", target_lang,
                     cfg.get("localize.tts_backend"))
    else:
        caption_transcript = transcript
        clip_lang = src_lang
    lang = clip_lang

    # --- M9 jump cuts: trim dead air, keeping video/audio/captions in sync ---- #
    jumpcuts_on = bool(cfg.get("edit.jumpcuts", False))
    if jumpcuts_on and dub_on:
        log.info("jump cuts disabled while dubbing (would desync the new voice)")
        jumpcuts_on = False
    if jumpcuts_on and not probe.has_audio:
        log.info("jump cuts need audio; source has none — skipping")
        jumpcuts_on = False
    if jumpcuts_on:
        log.info("jump cuts on: trimming silences > %.2fs",
                 float(cfg.get("edit.min_silence", 0.8)))

    use_track = mode == "track" and tracking_available()
    if mode == "track" and not use_track:
        log.info("subject tracking unavailable (opencv/model); using center-crop")
    log.info("reframe mode: %s%s", "track" if use_track else "center",
             " + logo" if logo else "")

    rendered: list[dict] = []
    for clip in clips:
        # Localized caption text drives captions, metadata, QC, and the manifest.
        if localize_on:
            loc_text = " ".join(
                w.text for w in caption_transcript.words_in(clip.start, clip.end)
            ).strip()
            if loc_text:
                clip.caption_text = loc_text

        # Deterministic naming: {slug}_{lang}_{clipid}_{yyyymmdd}.mp4  (M13)
        out_path = os.path.join(out_dir, f"{slug}_{lang}_{clip.clip_id}_{date}.mp4")

        # Resume: skip the expensive render if this exact output already exists.
        reuse = resume and os.path.isfile(out_path) and os.path.getsize(out_path) > 0

        keep_ranges = None
        dub_method = "none"
        track = None
        lipsynced = False
        if reuse:
            log.info("resume: clip %s already rendered, reusing %s",
                     clip.clip_id, os.path.basename(out_path))
        else:
            # Jump-cut plan from the ORIGINAL speech timing (real audio/video);
            # the translated caption words share that same source timeline, so one
            # plan drives video, audio and captions together.
            if jumpcuts_on:
                kr = plan_keep_ranges(
                    transcript.words(), clip.start, clip.end,
                    min_silence=float(cfg.get("edit.min_silence", 0.8)),
                    max_gap=float(cfg.get("edit.max_gap", 0.35)),
                    pad=float(cfg.get("edit.pad", 0.1)),
                )
                if total_kept(kr) < clip.duration - 0.3:  # only if it actually trims
                    keep_ranges = kr

            ass_path = build_ass(
                clip, caption_transcript, out_w, out_h, cfg,
                cache.path(f"clip_{clip.clip_id}_{clip_lang}.ass"),
                keep_ranges=keep_ranges,
            )
            subs = subtitles_filter(ass_path, fontsdir) if ass_path else None

            # M6 dub: voiceover over preserved music/SFX, if enabled.
            dub_audio = None
            if dub_on:
                dub_audio, dub_method = dub_clip(
                    meta.file_path, clip, caption_transcript, cfg, cache.path("dub")
                )

            video_select = None
            if keep_ranges:
                from .analyze.audio import select_expr
                video_select = select_expr(keep_ranges, clip.start)

            track = plan_track(meta.file_path, clip, out_w, out_h, cfg) if use_track else None
            if track is not None:
                fg = build_filtergraph(
                    probe.width, probe.height, out_w, out_h,
                    pre_cropped=True, subtitles=subs, logo=logo,
                )
                render_clip_tracked(
                    meta.file_path, clip, track, out_w, out_h, fg, cfg, out_path,
                    audio_path=dub_audio, keep_ranges=keep_ranges,
                )
            else:
                fg = build_filtergraph(
                    probe.width, probe.height, out_w, out_h,
                    fill=fill, subtitles=subs, logo=logo, video_select=video_select,
                )
                render_clip(meta.file_path, clip, fg, cfg, out_path,
                            audio_path=dub_audio, keep_ranges=keep_ranges)

            # Lip-sync the dubbed clip so the mouth tracks the new voiceover.
            if lipsync_on and dub_audio:
                lipsynced = lipsync_clip(out_path, dub_audio, cfg, out_path)
        clip.file_path = os.path.abspath(out_path)

        # M10 title/description/tags — generated for every output video.
        md = gen_metadata(clip, cfg) if do_meta else None
        # M14 publishing sidecar: copy-paste-ready title/description/tags next
        # to the video, so they travel with each clip (not just in the manifest).
        publish_file = write_sidecar(out_path, md) if (md and do_publish) else None

        thumb = None
        if do_thumb:
            thumb_path = os.path.join(out_dir, f"{slug}_{lang}_{clip.clip_id}_{date}.jpg")
            if reuse and os.path.isfile(thumb_path):
                thumb = thumb_path
            else:
                thumb = make_thumbnail(
                    meta.file_path, clip, out_w, out_h, cfg, thumb_path,
                    track=track, text_hook=(md or {}).get("title"),
                )

        entry = clip.to_dict()
        entry["tracked"] = track is not None
        entry["language"] = clip_lang
        if reuse:
            entry["reused"] = True
        if keep_ranges:
            entry["jumpcut"] = True
            entry["edited_duration"] = round(total_kept(keep_ranges), 3)
        if localize_on:
            entry["dub_method"] = dub_method
            entry["lipsynced"] = lipsynced
        if md:
            entry["metadata"] = md
        if publish_file:
            entry["publish_file"] = os.path.abspath(publish_file)
        if thumb:
            entry["thumbnail"] = os.path.abspath(thumb)
        if do_review:
            entry["review"] = review_clip(clip.caption_text, clip_lang, dub_method)
        rendered.append(entry)

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
            "reframe_mode": "track" if use_track else "center",
            "fill": fill,
            "target_duration": cfg.get("select.target_duration"),
            "caption_template": _cap_style.get("_template") if _cap_style else None,
            "caption_animation": _cap_style.get("animation") if _cap_style else None,
            "loudnorm": bool(cfg.get("render.loudnorm", True)),
            "jumpcuts": jumpcuts_on,
            "logo": bool(logo),
            "hook_backend": cfg.get("detect.backend"),
            "output_language": clip_lang,
            "localized": localize_on,
            "dubbed": dub_on,
            "lipsync": lipsync_on,
        },
        "recommendation": {"recommended_clips": n_rec, "rationale": rationale},
        "review": {
            "gate": "pending_review" if do_review else "disabled",
            "note": "Approve each clip before scheduling/publishing (Phase 5).",
            "flagged": sum(1 for c in rendered if c.get("review", {}).get("flags")),
        },
        "clips": rendered,
    }
    manifest_path = os.path.join(out_dir, f"{slug}_{date}_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    manifest["manifest_path"] = os.path.abspath(manifest_path)
    log.info("wrote manifest: %s", manifest_path)
    return manifest
