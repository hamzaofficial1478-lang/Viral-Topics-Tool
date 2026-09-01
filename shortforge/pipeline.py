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
from .localize.translate import resolve_backend
from .metadata import generate as gen_metadata
from .publish import write_sidecar
from .qc import review_clip
from .reframe import parse_aspect, plan_vcam, tracking_available
from .render import build_filtergraph, render_clip, render_clip_tracked
from .select import build_clips, recommend_clip_count
from .thumbnail import make_thumbnail
from .utils import ShortForgeError, ffprobe_info, log, require_binary

_RTL_LANGS = {"ar", "he", "fa", "ur"}


def _slug(text: str, fallback: str = "clip") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or fallback


def _clip_is_reusable(out_path: str, resume: bool) -> bool:
    """Whether a previously-rendered clip file can be trusted and reused
    instead of re-rendered.

    Existence + non-zero size alone isn't enough: ffmpeg writes straight to
    ``out_path`` with no temp-file+rename, so a crash mid-encode (power cut,
    kill) leaves a real, non-empty, CORRUPT file sitting at exactly the path
    a resumed run looks for — a bare existence check would trust it as "done"
    and silently ship a broken clip. Probe it instead: a truncated file
    either fails to probe at all or reports a near-zero duration.
    """
    if not (resume and os.path.isfile(out_path) and os.path.getsize(out_path) > 0):
        return False
    try:
        return ffprobe_info(out_path).duration > 1.0
    except Exception:  # noqa: BLE001 - unreadable/corrupt: treat as not reusable
        return False


def _find_fontsdir() -> str | None:
    for d in ("/usr/share/fonts", "/Library/Fonts", "/System/Library/Fonts"):
        if os.path.isdir(d):
            return d
    return None


def _dry_run_manifest(meta, clips, clip_lang, cost_estimate, out_dir, slug, date) -> dict:
    """STEP 1: --dry-run-cost — the plan + projected spend, nothing rendered."""
    manifest = {
        "source": {"source": meta.source, "title": meta.title,
                   "duration": round(meta.duration, 2), "hash": meta.hash},
        "dry_run": True,
        "output_language": clip_lang,
        "cost_estimate": cost_estimate.to_dict() if cost_estimate else None,
        "planned_clips": [
            {"clip_id": c.clip_id, "start": round(c.start, 2), "end": round(c.end, 2),
             "duration": round(c.duration, 2),
             "text": (c.caption_text or "")[:160]} for c in clips
        ],
        "summary": (f"DRY RUN: {len(clips)} clip(s) planned"
                    + (f" | est {cost_estimate.human()}" if cost_estimate else "")),
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{slug}_{date}_dryrun.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    manifest["manifest_path"] = os.path.abspath(path)
    log.info(manifest["summary"])
    return manifest


def run_pipeline(
    source: str,
    cfg: Config,
    *,
    owner_confirmed: bool,
    transcript_path: str | None = None,
    confirm_cost=None,
) -> dict:
    """Execute the full Phase 1 pipeline. Returns a manifest dict.

    ``confirm_cost`` (optional) is called with a CostEstimate before any paid
    synthesis; returning False aborts the run (STEP 1 cost controls).
    """
    from .doctor import preflight
    from . import timing
    preflight(cfg)  # C1: fail fast with actionable messages
    require_binary("ffmpeg")
    require_binary("ffprobe")

    # STEP 0: per-stage + per-ffmpeg timing for this run (so we optimize the real
    # bottleneck). Active only for this call; leaf stages/ffmpeg append to it.
    _timings = timing.Timings()
    _tok = timing.activate(_timings)

    work_dir = cfg.get("paths.work_dir", ".shortforge")
    out_dir = cfg.get("paths.output_dir", "out")
    os.makedirs(out_dir, exist_ok=True)

    # --- M1 ingest -------------------------------------------------------- #
    with timing.stage("ingest"):
        meta = ingest(source, cfg, owner_confirmed=owner_confirmed)
    cache = Cache(work_dir, meta.hash)

    # --- M2 transcribe (or use supplied transcript) ----------------------- #
    if transcript_path:
        with timing.stage("asr", note="supplied"):
            transcript = load_external_transcript(
                transcript_path, meta.duration, meta.src_lang or "en"
            )
            cache.save_json("transcript.json", transcript.to_dict())
        log.info("using supplied transcript: %s (%d segments)",
                 transcript_path, len(transcript.segments))
    else:
        _asr_cached = os.path.isfile(cache.path("transcript.json"))
        with timing.stage("asr"):
            transcript = transcribe(meta, cfg, cache)
        if _asr_cached:
            timing.note("asr", "cached")

    # A source with no speech (music, sports, drone footage, a silent capture) is
    # still a source: "cut this into clips of N seconds" is a complete instruction
    # on its own. Hook detection has nothing to bite on, so the timeline is used
    # instead — loudly, never as a silent substitution.
    from .select import nospeech
    speechless = not nospeech.has_speech(transcript)

    candidates: list = []
    if speechless:
        timing.note("hook-scoring", "no speech")
    else:
        # --- M3 detect (transcript + visual signals) ---------------------- #
        with timing.stage("hook-scoring"):
            candidates = detect_hooks(transcript, cfg, meta.file_path)

    # --- M4 select (with recommendation) ---------------------------------- #
    requested = int(cfg.get("select.num_clips", 0) or 0)
    if speechless:
        n_rec = requested or 3
        rationale = ("No speech in this source, so there are no spoken hooks to rank; "
                     "clips are cut from the timeline at the requested length.")
    else:
        n_rec, rationale = recommend_clip_count(transcript, candidates, cfg)
    log.info("recommendation: %s", rationale)
    if requested:
        log.info("operator requested %d clip%s", requested, "s" if requested != 1 else "")

    # Resolve output geometry once; keep selection + reframe in agreement.
    # Resolution (pixel short-side) is separate from aspect (shape).
    from .reframe import apply_resolution
    apply_resolution(cfg)
    out_w, out_h = parse_aspect(
        cfg.get("reframe.aspect", "9:16"),
        int(cfg.get("reframe.width", 1080)),
        int(cfg.get("reframe.height", 1920)),
    )
    cfg.override("reframe.width", out_w)
    cfg.override("reframe.height", out_h)

    selection_basis = "speech"
    with timing.stage("select"):
        if speechless:
            clips, selection_basis = nospeech.build_clips(meta, cfg, meta.hash)
        else:
            clips = build_clips(transcript, candidates, cfg, meta.hash)
    if not clips:
        raise ShortForgeError("No clips could be selected from this source.")
    log.info("selected %d clip%s", len(clips), "s" if len(clips) != 1 else "")

    # --- M5/M7/M8/M13 reframe + captions + branding + render -------------- #
    probe = ffprobe_info(meta.file_path)
    cfg.override("reframe._src_w", probe.width)
    cfg.override("reframe._src_h", probe.height)
    fill = cfg.get("reframe.fill", "crop")
    mode = cfg.get("reframe.mode", "center")
    fontsdir = _find_fontsdir()

    # Sharpness accounting. Cropping a 16:9 source to a vertical shape throws
    # away most of the width, and whatever is left gets enlarged to the export
    # size. Beyond ~1.15x that is visible as softness no encoder setting can
    # undo, so say it plainly (engineering rule 1: never silently degrade) and
    # record it in the manifest (rule 3) instead of shipping a soft clip and
    # letting the operator discover it on Facebook.
    from .reframe.resolution import upscale_factor, source_height_needed
    _res = cfg.get("reframe.resolution", "1080p")
    _asp = cfg.get("reframe.aspect", "9:16")
    upscale = upscale_factor(probe.width, probe.height, _res, _asp)
    sharpen_cfg = cfg.get("render.sharpen")
    sharpen = bool(upscale > 1.02) if sharpen_cfg is None else bool(sharpen_cfg)
    quality_note = None
    if upscale > 1.15:
        need = source_height_needed(_res, _asp)
        quality_note = (
            f"source is {probe.width}x{probe.height}; a {_asp} crop of it must be "
            f"enlarged {upscale:.2f}x to reach {out_w}x{out_h}, so these clips will "
            f"look soft. For a sharp {out_w}x{out_h} the source needs to be at least "
            f"{need}px tall (a 4K/2160p upload). Either pick a higher-resolution "
            f"source, or export at a size this source can actually fill."
        )
        log.warning("QUALITY: %s", quality_note)
    elif upscale > 1.02:
        log.info("source crop is being enlarged %.2fx; sharpening enabled", upscale)
    else:
        log.info("source is large enough for %dx%d (crop downscales %.2fx) — no upscaling",
                 out_w, out_h, 1.0 / max(upscale, 1e-6))

    # A3: detect captions baked into the source pixels; treat the band so our
    # target-language captions are the only text on screen.
    burned_mode = str(cfg.get("captions.burned_in", "none"))
    burned_band = None
    if burned_mode != "none":
        from .captions import burned_in
        burned_band = burned_in.detect(meta.file_path, probe.width, probe.height, cfg)
        if burned_band:
            log.info("burned-in captions: applying '%s' treatment to the detected band",
                     burned_mode)
    lang = transcript.language or "xx"
    date = _dt.date.today().strftime("%Y%m%d")
    slug = _slug(meta.title, "source")
    # Queue runs tag every output with which link produced it, so a folder of
    # clips from ten links stays readable (e.g. link03_my-video_en_01_20260806.mp4).
    _prefix = str(cfg.get("paths.output_prefix", "") or "").strip()
    if _prefix:
        slug = f"{_slug(_prefix, 'link')}_{slug}"

    logo = logo_spec(cfg, out_w, out_h)
    _cap_style = resolve_caption_style(cfg) if cfg.get("captions.enabled", True) else None
    if _cap_style:
        log.info("caption template: %s (%s animation)",
                 _cap_style["_template"], _cap_style["animation"])
    do_meta = bool(cfg.get("metadata.enabled", False))
    do_thumb = bool(cfg.get("thumbnail.enabled", False))
    do_review = bool(cfg.get("review.enabled", True))
    do_publish = bool(cfg.get("publish.sidecar", True))
    resume = bool(cfg.get("render.resume", False))

    # --- M6 localization: target-language captions + optional dub ---------- #
    target_lang = cfg.get("localize.language")
    src_lang = transcript.language or "en"
    localize_on = bool(target_lang) and str(target_lang).split("-")[0] != src_lang
    dub_on = localize_on and bool(cfg.get("localize.dub", False))

    # Cutting a silent video is fine; *translating* or *dubbing* one is not — there
    # are no words to translate and no voice to replace. Emitting a silent clip and
    # calling it a French dub is exactly the failure this project forbids, so say so
    # instead. (Captions need no such guard: with no words there is nothing to draw,
    # which is honest and visible.)
    if speechless and localize_on:
        raise ShortForgeError(nospeech.unsupported_with_speech_only(
            f"{'Dubbing' if dub_on else 'Caption translation'} to '{target_lang}'",
            "This source is music/ambience only. "))
    if speechless and bool(cfg.get("captions.enabled", True)):
        log.warning("no speech: clips will have no captions (there are no words to show).")

    # A1: dubbing must REMOVE the original voice (keep music/SFX). Resolve stem
    # separation up front and abort *before* any rendering rather than laying a
    # second voice over the original.
    accompaniment_source = None
    allow_voice_bleed = bool(cfg.get("localize.allow_voice_bleed", False))
    if not dub_on:
        timing.note("stems", "skipped")
    if dub_on:
        from .localize import stems
        should_stem, stem_reason = stems.stem_separation_enabled(cfg)
        if should_stem:
            src_wav = cache.path("source_48k.wav")
            with timing.stage("stems"):
                stem_result = stems.separate_source(
                    meta.file_path, src_wav, cache.dir,
                    transcript.duration or meta.duration, cfg,
                )
            if stem_result is not None:
                # G1: reconstruct the bed (accompaniment + retained ambience).
                accompaniment_source = stems.build_bed(stem_result, cache.dir, cfg)
                if bool(cfg.get("localize.debug_audio", False)):
                    stems.export_debug_audio(stem_result, accompaniment_source, out_dir)
            if accompaniment_source is None and not allow_voice_bleed:
                raise ShortForgeError(
                    "Dub requested but Demucs stem separation failed, so the "
                    "original voice cannot be removed. Fix Demucs (`pip install "
                    "-U demucs`) or pass --allow-voice-bleed to accept two voices."
                )
        elif not allow_voice_bleed:
            # Name the EXACT interpreter: a bare `pip install demucs` often lands in
            # the system Python while ShortForge runs in the venv, so the operator
            # sees "already satisfied" and this error at the same time.
            import sys as _sys
            raise ShortForgeError(
                f"Dub requested but stem separation is {stem_reason}. Removing the "
                f"original voice needs Demucs, installed into the SAME Python that "
                f"runs ShortForge:\n"
                f'    "{_sys.executable}" -m pip install demucs\n'
                f"(a bare `pip install demucs` can install into a different Python — "
                f"if pip says 'already satisfied' but you still see this, that's why.)\n"
                f"Verify with:  \"{_sys.executable}\" -c \"import demucs; print(demucs.__file__)\"\n"
                f"Or: pass --allow-voice-bleed to duck the original (two voices), or "
                f"drop the dub option for translated captions over the original audio."
            )

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
    translation_backend = "identity"
    if localize_on:
        allow_untranslated = bool(cfg.get("localize.allow_untranslated", False))
        no_cache = bool(cfg.get("cache.disabled", False))
        refresh = bool(cfg.get("cache.refresh_translation", False))
        # A2: provenance in the cache key — a failure can never masquerade as a hit.
        bname, bver = resolve_backend(cfg, src_lang, target_lang)
        if bname == "none" and not allow_untranslated:
            raise ShortForgeError(
                f"Output language '{target_lang}' differs from source '{src_lang}' "
                f"but no translation backend is available. Install argostranslate "
                f"(offline) or set ANTHROPIC_API_KEY, or pass --allow-untranslated. "
                f"(Silently emitting source-language captions is not allowed.)"
            )
        cache_key = f"transcript_{target_lang}_{_slug(bname)}_{_slug(bver)}.json"
        cached_tr = None if (no_cache or refresh) else cache.load_json(cache_key)
        if cached_tr:
            from .models import Transcript
            caption_transcript = Transcript.from_dict(cached_tr)
            translation_backend = bname
            log.info("using cached %s translation (%s)", target_lang, bname)
        else:
            log.info("translating captions %s -> %s (%s)", src_lang, target_lang, bname)
            caption_transcript, tr_result = build_translated_transcript(
                transcript, target_lang, cfg, allow_untranslated)
            translation_backend = tr_result.backend
            # A2: never cache a failed/passthrough/suspect translation.
            if tr_result.cacheable and not no_cache:
                cache.save_json(cache_key, caption_transcript.to_dict())
            else:
                log.info("not caching translation (passthrough/suspect or --no-cache)")
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
    if jumpcuts_on and speechless:
        # Jump cuts trim the gaps *between words*. With no words every frame is a
        # gap, so the plan would cut the clip down to nothing.
        log.info("jump cuts need speech to find the gaps; this source has none — skipping")
        jumpcuts_on = False
    if jumpcuts_on:
        log.info("jump cuts on: trimming silences > %.2fs",
                 float(cfg.get("edit.min_silence", 0.8)))

    # Orientation gate: face tracking exists to keep a speaker in frame when a
    # LANDSCAPE source is squeezed into a PORTRAIT crop. Exporting landscape or
    # square doesn't need it — skip the per-frame CV pass entirely and centre-crop,
    # which is both faster and steadier. Override with reframe.track_when.
    portrait_out = out_h > out_w
    track_when = str(cfg.get("reframe.track_when", "portrait")).lower()
    orientation_ok = portrait_out if track_when == "portrait" else True
    use_track = mode == "track" and orientation_ok and tracking_available()
    if mode == "track" and not orientation_ok:
        log.info("reframe: %dx%d output is not portrait — skipping face tracking "
                 "(centre crop; no per-frame vision work)", out_w, out_h)
    elif mode == "track" and not use_track:
        log.info("subject tracking unavailable (opencv/model); using center-crop")
    log.info("reframe mode: %s%s", "track" if use_track else "center",
             " + logo" if logo else "")

    # --- STEP 3: fail loud if no voice provider covers the dub language ----- #
    if dub_on:
        from .providers import dub_language_check
        from .providers.store import load_store
        ok_lang, lang_msg = dub_language_check(load_store(), clip_lang)
        if not ok_lang:
            raise ShortForgeError(
                lang_msg + " (Requesting a dub in an unsupported language is refused "
                "rather than synthesized in the wrong language or a flat fallback.)")

    # --- STEP 1: cost pre-flight (before any paid synthesis) ---------------- #
    from .cost import estimate_tts
    from .providers import build_tts_router
    cost_estimate = None
    if dub_on:
        dub_texts: list[str] = []
        for clip in clips:
            for s in caption_transcript.segments:
                if s.end > clip.start and s.start < clip.end and s.text.strip():
                    dub_texts.append(s.text)
        router = build_tts_router(cache.path("tts"))
        cost_estimate = estimate_tts(dub_texts, router, clip_lang)
        log.info("cost estimate: %s", cost_estimate.human())
        ceiling = float(cfg.get("cost.max_usd_per_job", 0) or 0)
        if ceiling > 0 and cost_estimate.cost_usd > ceiling:
            raise ShortForgeError(
                f"Estimated dub cost ${cost_estimate.cost_usd:g} exceeds the per-job "
                f"ceiling ${ceiling:g}. Raise cost.max_usd_per_job / --cost-ceiling, "
                f"reduce clips, or use a cheaper voice provider.")
        if confirm_cost is not None and cost_estimate.cost_usd > 0:
            if not confirm_cost(cost_estimate):
                raise ShortForgeError("Cancelled at cost confirmation.")
    if bool(cfg.get("cost.dry_run", False)):
        log.info("--dry-run-cost: reporting the plan only, no synthesis or render.")
        timing.deactivate(_tok)
        return _dry_run_manifest(meta, clips, clip_lang, cost_estimate, out_dir, slug, date)

    # STEP 5: render clips concurrently — each ffmpeg encode is CPU-bound and the
    # clips are independent. Deferred to a worker pool below (the dub path keeps
    # rendering inline: it shares audio/lipsync state that isn't parallel-safe).
    from .render import plan_render, resolve_encoder, available_hw_encoders
    # STEP 1: warm the encoder cache in the main thread (only when a hardware
    # encoder is actually requested, so the default x264 run does no extra probe)
    # — avoids parallel workers each shelling out to `ffmpeg -encoders`.
    if str(cfg.get("render.encoder", "x264") or "x264").lower() not in ("x264", "libx264", "software", "cpu"):
        available_hw_encoders()
    render_workers, render_threads = plan_render(cfg, len(clips))
    parallel_render = render_workers > 1 and len(clips) > 1 and not dub_on
    if parallel_render and render_threads:
        cfg.override("render.threads", render_threads)
    render_jobs: list = []           # (clip_id, thunk) deferred renders (parallel path)

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

        # Resume: skip the expensive render if this exact output already exists
        # and passes a real validity check (see _clip_is_reusable).
        reuse = _clip_is_reusable(out_path, resume)
        if resume and not reuse and os.path.isfile(out_path):
            log.warning("resume: %s exists but looks incomplete (interrupted mid-render?) "
                       "— re-rendering instead of reusing it", os.path.basename(out_path))

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

            with timing.stage("captions"):
                ass_path = build_ass(
                    clip, caption_transcript, out_w, out_h, cfg,
                    cache.path(f"clip_{clip.clip_id}_{clip_lang}.ass"),
                    keep_ranges=keep_ranges,
                )
            subs = subtitles_filter(ass_path, fontsdir) if ass_path else None

            # M6 dub: voiceover over preserved music/SFX, if enabled.
            dub_audio = None
            if dub_on:
                # Hard guard: TTS/dub only ever runs for a cross-language request.
                # In the default (original-audio) path this branch is never entered,
                # so no TTS provider is resolved or called — nothing to spend.
                assert localize_on, "dub invoked without a cross-language request (bug)"
                with timing.stage("dub"):
                    dub_audio, dub_method = dub_clip(
                        meta.file_path, clip, caption_transcript, cfg, cache.path("dub"),
                        accompaniment_source=accompaniment_source,
                        allow_voice_bleed=allow_voice_bleed,
                    )

            video_select = None
            if keep_ranges:
                from .analyze.audio import select_expr
                video_select = select_expr(keep_ranges, clip.start)

            # Reframe planning = per-frame saliency + scene-cut detection (the CV
            # pass); timed as "saliency" since it dominates the plan.
            track = None
            if use_track:
                with timing.stage("saliency"):
                    track = plan_vcam(meta.file_path, clip, out_w, out_h, cfg, cache)
            if use_track and bool(cfg.get("reframe.debug", False)):
                from .reframe.vcam import debug_reframe
                dbg = os.path.join(out_dir, f"{slug}_{lang}_{clip.clip_id}_{date}_reframe-debug.mp4")
                debug_reframe(meta.file_path, clip, out_w, out_h, cfg, cache, dbg)
            if track is not None:
                fg = build_filtergraph(
                    probe.width, probe.height, out_w, out_h,
                    pre_cropped=True, subtitles=subs, logo=logo, sharpen=sharpen,
                )

                def _do_render(clip=clip, track=track, fg=fg, out_path=out_path,
                               dub_audio=dub_audio, keep_ranges=keep_ranges):
                    render_clip_tracked(
                        meta.file_path, clip, track, out_w, out_h, fg, cfg, out_path,
                        audio_path=dub_audio, keep_ranges=keep_ranges,
                        burned_band=burned_band, burned_mode=burned_mode,
                    )
            else:
                fg = build_filtergraph(
                    probe.width, probe.height, out_w, out_h,
                    fill=fill, subtitles=subs, logo=logo, video_select=video_select,
                    burned_band=burned_band, burned_mode=burned_mode, sharpen=sharpen,
                )

                def _do_render(clip=clip, fg=fg, out_path=out_path,
                               dub_audio=dub_audio, keep_ranges=keep_ranges):
                    render_clip(meta.file_path, clip, fg, cfg, out_path,
                                audio_path=dub_audio, keep_ranges=keep_ranges)

            if parallel_render:
                render_jobs.append((clip.clip_id, _do_render))   # rendered in the pool below
            else:
                with timing.stage("render"):
                    _do_render()

            # Lip-sync the dubbed clip so the mouth tracks the new voiceover.
            if lipsync_on and dub_audio:
                lipsynced = lipsync_clip(out_path, dub_audio, cfg, out_path)
        clip.file_path = os.path.abspath(out_path)

        # M10 title/description/tags — generated for every output video.
        md = None
        if do_meta:
            with timing.stage("metadata"):
                md = gen_metadata(clip, cfg)
        # M14 publishing sidecar: copy-paste-ready title/description/tags next
        # to the video, so they travel with each clip (not just in the manifest).
        publish_file = write_sidecar(out_path, md) if (md and do_publish) else None

        thumb = None
        if do_thumb:
            thumb_path = os.path.join(out_dir, f"{slug}_{lang}_{clip.clip_id}_{date}.jpg")
            if reuse and os.path.isfile(thumb_path):
                thumb = thumb_path
            else:
                with timing.stage("thumbnail"):
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

    # STEP 5: run the deferred clip renders concurrently. Only the parallel path
    # populates render_jobs (the dub path renders inline above). Timed as one
    # "render" bucket = wall-clock; each ffmpeg still logs/records itself.
    if render_jobs:
        from concurrent.futures import ThreadPoolExecutor

        def _run_job(item):
            """Never raise: one bad clip must not throw away the whole run (every
            other clip is already fully analysed and rendered by this point)."""
            clip_id, thunk = item
            timing.activate(_timings)     # per-thread: record ffmpeg into the shared Timings
            try:
                thunk()
                return clip_id, None
            except Exception as e:  # noqa: BLE001
                return clip_id, e

        log.info("rendering %d clip(s) in parallel: %d workers, %s threads/ffmpeg each",
                 len(render_jobs), render_workers, render_threads or "auto")
        with timing.stage("render"):
            with ThreadPoolExecutor(max_workers=render_workers) as ex:
                results_r = list(ex.map(_run_job, render_jobs))

        failed_ids = {cid for cid, err in results_r if err is not None}
        for cid, err in results_r:
            if err is not None:
                log.error("clip %s failed to render: %s", cid, err)
        if failed_ids:
            if len(failed_ids) == len(render_jobs):
                raise ShortForgeError(
                    "Every clip failed to render. First error: "
                    f"{next(e for _, e in results_r if e is not None)}")
            # Deliver what DID render; drop the failures from the manifest so it
            # never points at a file that doesn't exist.
            rendered = [e for e in rendered if e.get("clip_id") not in failed_ids]
            log.warning("%d of %d clip(s) failed to render — CONTINUING with the %d that "
                        "succeeded (failed: %s)", len(failed_ids), len(render_jobs),
                        len(rendered), ", ".join(sorted(failed_ids)))

    if not do_meta:
        timing.note("metadata", "off")
    if not do_thumb:
        timing.note("thumbnail", "off")

    # --- B: one-line provenance summary (required; how the operator verifies
    #        which code path actually ran) ---------------------------------- #
    captions_on = bool(cfg.get("captions.enabled", True))
    if dub_on:
        if accompaniment_source:
            _strength = str(cfg.get("localize.vocal_removal_strength", "partial")).lower()
            stem_state = "stems d+b+o" + ("" if _strength in ("full", "off", "none") else "+amb")
        else:
            stem_state = "voice-bleed" if allow_voice_bleed else "no-stems"
        dub_desc = f"dub ({cfg.get('localize.tts_backend', 'auto')}, {stem_state})"
    else:
        dub_desc = "dub: none (original audio)"
    lang_desc = (f"lang {src_lang}->{clip_lang} ({translation_backend})"
                 if localize_on else f"lang {src_lang}")
    summary = " | ".join([
        f"done: {len(rendered)} clip(s)",
        # B: the summary line must name the path actually taken. "no speech" is a
        # materially different selection, so it can never be read as a hook run.
        f"selection {selection_basis}" if speechless else "selection hooks",
        lang_desc,
        dub_desc,
        f"captions {'none (no speech)' if speechless else clip_lang if captions_on else 'off'}",
        f"reframe {'track' if use_track else 'center'}",
        f"encoder {resolve_encoder(cfg)} q={cfg.get('render.quality', 'high')}",
        (f"source {probe.width}x{probe.height} (upscaled {upscale:.2f}x)"
         if upscale > 1.02 else f"source {probe.width}x{probe.height}"),
        f"metadata {'on' if do_meta else 'off'}",
        f"thumbnail {'on' if do_thumb else 'off'}",
    ] + (["jumpcuts"] if jumpcuts_on else [])
      + (["lipsync"] if lipsync_on else [])
      + ([f"cost ~${cost_estimate.cost_usd:.2f}"] if cost_estimate and cost_estimate.priced else []))
    log.info(summary)

    # --- manifest --------------------------------------------------------- #
    manifest = {
        "source": {
            "source": meta.source,
            "title": meta.title,
            "duration": round(meta.duration, 2),
            "language": transcript.language,
            "hash": meta.hash,
        },
        "summary": summary,
        "settings": {
            "resolution": f"{out_w}x{out_h}",
            "aspect": cfg.get("reframe.aspect"),
            # Sharpness provenance: what the source could actually supply, and
            # whether the export had to invent pixels to reach the chosen size.
            "source_resolution": f"{probe.width}x{probe.height}",
            "upscale_factor": round(upscale, 3),
            "sharpened": bool(sharpen),
            "quality": cfg.get("render.quality", "high"),
            "quality_warning": quality_note,
            "reframe_mode": "track" if use_track else "center",
            "fill": fill,
            "target_duration": cfg.get("select.target_duration"),
            "caption_template": _cap_style.get("_template") if _cap_style else None,
            "caption_animation": _cap_style.get("animation") if _cap_style else None,
            "captions_translated": localize_on and captions_on,
            "loudnorm": bool(cfg.get("render.loudnorm", True)),
            "jumpcuts": jumpcuts_on,
            "logo": bool(logo),
            "hook_backend": cfg.get("detect.backend") if not speechless else None,
            "has_speech": not speechless,
            "selection_basis": selection_basis,
            "output_language": clip_lang,
            "localized": localize_on,
            "dubbed": dub_on,
            "dub_tts": cfg.get("localize.tts_backend") if dub_on else None,
            "lipsync": lipsync_on,
            "translation_backend": translation_backend,
            "stems_separated": bool(accompaniment_source),
            "burned_in": burned_mode if burned_band else "none",
        },
        "cost_estimate": cost_estimate.to_dict() if cost_estimate else None,
        "recommendation": {"recommended_clips": n_rec, "rationale": rationale},
        "review": {
            "gate": "pending_review" if do_review else "disabled",
            "note": "Approve each clip before scheduling/publishing (Phase 5).",
            "flagged": sum(1 for c in rendered if c.get("review", {}).get("flags")),
        },
        "clips": rendered,
    }
    # STEP 0: per-stage + per-ffmpeg timing into the manifest, logged, and shown
    # in the UI — so we optimize the measured bottleneck, not the assumed one.
    manifest["timings"] = _timings.to_dict()
    manifest["timings_summary"] = _timings.summary_line()
    log.info(manifest["timings_summary"])

    manifest_path = os.path.join(out_dir, f"{slug}_{date}_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    manifest["manifest_path"] = os.path.abspath(manifest_path)
    log.info("wrote manifest: %s", manifest_path)
    timing.deactivate(_tok)
    return manifest
