#!/usr/bin/env python3
"""ShortForge CLI — Phase 1 core clipper.

    python cli.py run <video-url-or-path> --owner-confirmed
    python cli.py wizard        # interactive, Section 7 order

Also runnable as `python -m shortforge`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys

from shortforge.captions.templates import ANIMATIONS, list_templates
from shortforge.config import Config
from shortforge.pipeline import run_pipeline
from shortforge.publish import write_index
from shortforge.utils import ShortForgeError, load_env_file, setup_logging, log


def _apply_common_overrides(cfg: Config, args: argparse.Namespace) -> None:
    cfg.override("select.target_duration", getattr(args, "duration", None))
    cfg.override("select.tolerance", getattr(args, "tolerance", None))
    cfg.override("select.num_clips", getattr(args, "num_clips", None))
    cfg.override("reframe.aspect", getattr(args, "aspect", None))
    cfg.override("reframe.fill", getattr(args, "fill", None))
    cfg.override("transcribe.model", getattr(args, "whisper_model", None))
    cfg.override("paths.work_dir", getattr(args, "work_dir", None))
    cfg.override("paths.output_dir", getattr(args, "output", None))
    cfg.override("ingest.cookies", getattr(args, "cookies", None))
    cfg.override("reframe.mode", getattr(args, "reframe_mode", None))
    cfg.override("captions.style", getattr(args, "caption_style", None))
    cfg.override("captions.template", getattr(args, "caption_template", None))
    cfg.override("captions.animation", getattr(args, "caption_animation", None))
    cfg.override("captions.font", getattr(args, "caption_font", None))
    cfg.override("captions.font_size", getattr(args, "caption_size", None))
    cfg.override("captions.highlight_color", getattr(args, "highlight_color", None))
    if getattr(args, "uppercase", False):
        cfg.override("captions.uppercase", True)
    cfg.override("brand.logo", getattr(args, "logo", None))
    cfg.override("brand.corner", getattr(args, "logo_corner", None))
    cfg.override("brand.opacity", getattr(args, "logo_opacity", None))
    cfg.override("page.niche", getattr(args, "niche", None))
    if getattr(args, "vision", False):
        cfg.override("detect.vision_llm", True)
    if getattr(args, "no_visual", False):
        cfg.override("detect.visual", False)
    cfg.override("localize.language", getattr(args, "language", None))
    cfg.override("localize.tts_backend", getattr(args, "tts", None))
    cfg.override("localize.voice_sample", getattr(args, "voice_sample", None))
    cfg.override("localize.translate_backend", getattr(args, "translate_backend", None))
    if getattr(args, "dub", False):
        cfg.override("localize.dub", True)
    if getattr(args, "lipsync", False):
        cfg.override("lipsync.enabled", True)
    cfg.override("lipsync.wav2lip_repo", getattr(args, "wav2lip_repo", None))
    cfg.override("lipsync.checkpoint", getattr(args, "wav2lip_checkpoint", None))
    if getattr(args, "no_review", False):
        cfg.override("review.enabled", False)
    if getattr(args, "no_captions", False):
        cfg.override("captions.enabled", False)
    if getattr(args, "no_loudnorm", False):
        cfg.override("render.loudnorm", False)
    if getattr(args, "jumpcuts", False):
        cfg.override("edit.jumpcuts", True)
    if getattr(args, "no_metadata", False):
        cfg.override("metadata.enabled", False)
    if getattr(args, "no_thumbnail", False):
        cfg.override("thumbnail.enabled", False)
    if getattr(args, "no_sidecar", False):
        cfg.override("publish.sidecar", False)
    if getattr(args, "resume", False):
        cfg.override("render.resume", True)
    backend = getattr(args, "backend", None)
    if backend:
        cfg.override("detect.backend", backend)


def _print_summary(manifest: dict) -> None:
    src = manifest["source"]
    print("\n" + "=" * 64)
    print(f"  Source : {src['title'] or src['source']}")
    print(f"  Length : {src['duration']:.0f}s   Language: {src['language']}")
    print(f"  Output : {manifest['settings']['resolution']}  "
          f"({manifest['settings']['aspect']}, fill={manifest['settings']['fill']})")
    print(f"  {manifest['recommendation']['rationale']}")
    print("-" * 64)
    for c in manifest["clips"]:
        print(f"  clip {c['clip_id']}  [{c['start']:.1f}-{c['end']:.1f}]  "
              f"{c['duration']:.1f}s  score={c['score']:.2f}")
        print(f"           {c['file_path']}")
        if c.get("reason"):
            print(f"           why: {c['reason']}")
    print("-" * 64)
    print(f"  {len(manifest['clips'])} clip(s) + manifest -> {manifest['manifest_path']}")
    print("=" * 64)


def cmd_run(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    cfg = Config.load(args.config)
    _apply_common_overrides(cfg, args)
    try:
        manifest = run_pipeline(
            args.source,
            cfg,
            owner_confirmed=args.owner_confirmed,
            transcript_path=args.transcript,
        )
    except ShortForgeError as e:
        log.error("%s", e)
        return 2
    _print_summary(manifest)
    return 0


def _collect_batch_sources(args: argparse.Namespace) -> list[str]:
    sources = list(args.sources or [])
    if getattr(args, "from_file", None):
        with open(args.from_file, "r", encoding="utf-8") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                if s:
                    sources.append(s)
    return sources


def cmd_batch(args: argparse.Namespace) -> int:
    """Run the pipeline over several of the operator's own videos, in sequence.

    Deliberately lean: no job queue, no database — just a loop that keeps going
    when one source fails, then writes one combined index. That covers the real
    scale need (a backlog of your own videos) without the ops overhead.
    """
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")

    sources = _collect_batch_sources(args)
    if not sources:
        log.error("run-batch needs at least one source (positional args or --from-file).")
        return 2
    if not args.owner_confirmed:
        log.error("Batch requires --owner-confirmed (it applies to every source).")
        return 2
    if getattr(args, "transcript", None):
        log.warning("--transcript is ignored in batch mode (one file can't fit every source)")

    out_dir = None
    results: list[dict] = []
    failures: list[dict] = []
    for i, src in enumerate(sources, 1):
        log.info("=== batch %d/%d: %s ===", i, len(sources), src)
        cfg = Config.load(args.config)
        _apply_common_overrides(cfg, args)
        out_dir = cfg.get("paths.output_dir", "out")
        try:
            manifest = run_pipeline(src, cfg, owner_confirmed=True, transcript_path=None)
        except ShortForgeError as e:
            log.error("source failed (%s): %s", src, e)
            failures.append({"source": src, "error": str(e)})
            continue
        _print_summary(manifest)
        results.append(manifest)

    total_clips = sum(len(m["clips"]) for m in results)
    index = {
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "sources_requested": len(sources),
        "sources_succeeded": len(results),
        "sources_failed": len(failures),
        "total_clips": total_clips,
        "failures": failures,
        "runs": [
            {
                "source": m["source"]["source"],
                "title": m["source"]["title"],
                "clips": len(m["clips"]),
                "manifest": m.get("manifest_path"),
            }
            for m in results
        ],
    }
    index_path = None
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        name = f"batch_{_dt.date.today():%Y%m%d}_index.json"
        index_path = write_index(index, out_dir, name)

    print("\n" + "#" * 64)
    print(f"  BATCH: {len(results)}/{len(sources)} source(s) OK, "
          f"{len(failures)} failed  ->  {total_clips} clip(s) total")
    if failures:
        for f in failures:
            print(f"   ✗ {f['source']}: {f['error']}")
    if index_path:
        print(f"  index -> {os.path.abspath(index_path)}")
    print("#" * 64)
    return 0 if not failures else 1


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or (default or "")


def cmd_wizard(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    cfg = Config.load(args.config)
    print("ShortForge wizard — answer the prompts (Enter accepts the default).\n")

    # Section 7 operator input wizard order.
    source = _ask("1. Source video URL or local path")
    if not source:
        log.error("A source is required.")
        return 2
    owner = _ask("   Confirm this is YOUR content (yes/no)", "yes").lower().startswith("y")
    if not owner:
        log.error("Ownership is required — ShortForge only processes your own content.")
        return 2

    aspect = _ask("2. Aspect/resolution (9:16 / 1:1 / 16:9 / WxH)", "9:16")
    duration = _ask("3. Target clip duration seconds (30/45/60)", "45")
    num = _ask("4. Number of clips (0 = let the tool recommend)", "0")
    language = _ask("5. Output language (en/de/it/es/ja/ar, blank = keep source)", "")
    dub_mode = _ask("6. Dub mode (captions / voice / clone)", "captions")
    tts = _ask("   TTS backend (auto / espeak / edge / xtts)", "auto")
    transcript = _ask("7. Transcript (auto / path to .srt|.json)", "auto")
    caps = _ask("8. Burn captions? (yes/no)", "yes").lower().startswith("y")
    template = _ask("   Caption template (" + " / ".join(list_templates()) + ")", "clean")
    animation = _ask("   Caption animation (" + " / ".join(ANIMATIONS) + ", blank = template)", "")
    caption_font = _ask("   Caption font / text form (blank = template)", "")
    fill = _ask("   Reframe fill (crop / blur)", "crop")
    jumpcuts = _ask("9. Trim dead air with jump cuts? (yes/no)", "no").lower().startswith("y")
    output = _ask("12. Output directory", cfg.get("paths.output_dir", "out"))

    cfg.override("reframe.aspect", aspect)
    cfg.override("reframe.fill", fill)
    cfg.override("select.target_duration", _to_int(duration, 45))
    cfg.override("select.num_clips", _to_int(num, 0))
    cfg.override("captions.enabled", caps)
    cfg.override("captions.template", template or None)
    cfg.override("captions.animation", animation or None)
    cfg.override("captions.font", caption_font or None)
    cfg.override("edit.jumpcuts", jumpcuts)
    cfg.override("paths.output_dir", output)
    cfg.override("localize.language", language or None)
    if language and not dub_mode.lower().startswith("caption"):
        cfg.override("localize.dub", True)
        cfg.override("localize.tts_backend", "xtts" if dub_mode.lower().startswith("clone") else tts)
        lip = _ask("   Lip-sync the dub to the speaker's mouth? (yes/no) — needs Wav2Lip + GPU",
                   "no").lower().startswith("y")
        if lip:
            cfg.override("lipsync.enabled", True)
            cfg.override("lipsync.wav2lip_repo", _ask("     Path to Wav2Lip checkout", "") or None)
            cfg.override("lipsync.checkpoint", _ask("     Path to wav2lip_gan.pth", "") or None)

    transcript_path = None if transcript.lower() in ("", "auto") else transcript

    try:
        manifest = run_pipeline(
            source, cfg, owner_confirmed=True, transcript_path=transcript_path
        )
    except ShortForgeError as e:
        log.error("%s", e)
        return 2
    _print_summary(manifest)
    return 0


def _to_int(text: str, default: int) -> int:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shortforge",
        description="Turn your own long-form videos into short vertical clips (Phase 1).",
    )
    p.add_argument("--config", help="Path to settings.yaml (defaults to config/settings.yaml)")
    p.add_argument("--env-file", help="Path to a .env with API keys (defaults to ./.env)")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Run the pipeline non-interactively")
    r.add_argument("source", help="Your video: a URL or a local file path")
    _add_run_options(r)
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("run-batch",
                       help="Run the pipeline over several of your own videos (sequential)")
    b.add_argument("sources", nargs="*",
                   help="Your videos: URLs or local paths (or use --from-file)")
    b.add_argument("--from-file",
                   help="Text file with one source per line (# comments allowed)")
    _add_run_options(b)
    b.set_defaults(func=cmd_batch)

    w = sub.add_parser("wizard", help="Interactive operator wizard")
    w.set_defaults(func=cmd_wizard)
    return p


def _add_run_options(r: argparse.ArgumentParser) -> None:
    """Flags shared by `run` and `run-batch` (everything except the source(s))."""
    r.add_argument("--owner-confirmed", action="store_true",
                   help="Confirm the source is your own / licensed content (required)")
    r.add_argument("--duration", type=int, help="Target clip duration in seconds")
    r.add_argument("--tolerance", type=int, help="Allowed +/- seconds from target")
    r.add_argument("--num-clips", type=int, help="Number of clips (0 = auto-recommend)")
    r.add_argument("--aspect", help="9:16 / 1:1 / 16:9 / WxH (e.g. 1080x1920)")
    r.add_argument("--fill", choices=["crop", "blur"], help="Reframe fill mode")
    r.add_argument("--reframe-mode", choices=["track", "center"],
                   help="track = follow the speaker (M5); center = static crop")
    r.add_argument("--caption-template", choices=list_templates(),
                   help="Named caption look: " + " | ".join(list_templates()))
    r.add_argument("--caption-animation", choices=ANIMATIONS,
                   help="Caption animation (overrides template default)")
    r.add_argument("--caption-font", help="Override the caption font (text form)")
    r.add_argument("--caption-size", type=int, help="Override caption font size (px)")
    r.add_argument("--highlight-color", help="Override sung-word colour (ASS &HAABBGGRR)")
    r.add_argument("--uppercase", action="store_true", help="Force UPPERCASE captions")
    r.add_argument("--caption-style", choices=["karaoke", "simple"],
                   help="legacy shorthand (maps to --caption-animation)")
    r.add_argument("--logo", help="Path to a logo image to overlay (M8)")
    r.add_argument("--logo-corner", choices=["TL", "TR", "BL", "BR"], help="Logo corner")
    r.add_argument("--logo-opacity", type=float, help="Logo opacity 0..1")
    r.add_argument("--niche", help="Niche keyword(s) for metadata/hashtags (M10)")
    # Localization (Phase 3, M6)
    r.add_argument("--language", help="Target language code for captions/dub "
                   "(en/de/it/es/ja/ar/...); omit to keep the source language")
    r.add_argument("--dub", action="store_true",
                   help="Synthesize a voiceover in --language (else captions-only)")
    r.add_argument("--tts", choices=["auto", "espeak", "edge", "xtts"],
                   help="TTS backend: espeak (offline) / edge (natural) / xtts (clone, GPU)")
    r.add_argument("--voice-sample", help="Your-voice reference clip for --tts xtts")
    r.add_argument("--translate-backend", choices=["auto", "llm", "argos"],
                   help="Translation backend (Claude / offline Argos)")
    r.add_argument("--lipsync", action="store_true",
                   help="Lip-sync dubbed clips to the new voice (Wav2Lip; GPU, cross-language)")
    r.add_argument("--wav2lip-repo", help="Path to a cloned Rudrabha/Wav2Lip checkout")
    r.add_argument("--wav2lip-checkpoint", help="Path to wav2lip_gan.pth")
    r.add_argument("--no-review", action="store_true", help="Skip the QC review gate")
    r.add_argument("--no-captions", action="store_true", help="Do not burn captions")
    r.add_argument("--no-loudnorm", action="store_true", help="Skip -14 LUFS normalisation")
    r.add_argument("--jumpcuts", action="store_true",
                   help="Trim long silences / dead air (jump cuts); off when dubbing")
    r.add_argument("--no-metadata", action="store_true", help="Skip metadata generation")
    r.add_argument("--no-thumbnail", action="store_true", help="Skip thumbnail generation")
    r.add_argument("--no-sidecar", action="store_true",
                   help="Skip the per-video title/description/tags .txt file")
    r.add_argument("--resume", action="store_true",
                   help="Skip clips whose output file already exists")
    r.add_argument("--whisper-model", help="tiny|base|small|medium|large-v3")
    r.add_argument("--cookies", help="Path to cookies.txt for private/unlisted "
                   "videos on your own channel (M1 auth)")
    r.add_argument("--transcript", help="Use a supplied .srt/.json instead of ASR")
    r.add_argument("--vision", action="store_true",
                   help="Use Claude-vision multimodal hook scoring (claude-watch; needs key)")
    r.add_argument("--no-visual", action="store_true",
                   help="Disable local visual hook signals (motion/cuts/faces)")
    r.add_argument("--use-llm", dest="backend", action="store_const", const="llm",
                   help="Force Claude hook detection (needs ANTHROPIC_API_KEY)")
    r.add_argument("--no-llm", dest="backend", action="store_const", const="heuristic",
                   help="Force the keyword heuristic")
    r.add_argument("--work-dir", help="Cache/intermediate directory")
    r.add_argument("--output", help="Output directory for rendered clips")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
