#!/usr/bin/env python3
"""ShortForge CLI — Phase 1 core clipper.

    python cli.py run <video-url-or-path> --owner-confirmed
    python cli.py wizard        # interactive, Section 7 order

Also runnable as `python -m shortforge`.
"""

from __future__ import annotations

import argparse
import sys

from shortforge.config import Config
from shortforge.pipeline import run_pipeline
from shortforge.utils import ShortForgeError, setup_logging, log


def _apply_common_overrides(cfg: Config, args: argparse.Namespace) -> None:
    cfg.override("select.target_duration", getattr(args, "duration", None))
    cfg.override("select.tolerance", getattr(args, "tolerance", None))
    cfg.override("select.num_clips", getattr(args, "num_clips", None))
    cfg.override("reframe.aspect", getattr(args, "aspect", None))
    cfg.override("reframe.fill", getattr(args, "fill", None))
    cfg.override("transcribe.model", getattr(args, "whisper_model", None))
    cfg.override("paths.work_dir", getattr(args, "work_dir", None))
    cfg.override("paths.output_dir", getattr(args, "output", None))
    if getattr(args, "no_captions", False):
        cfg.override("captions.enabled", False)
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


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or (default or "")


def cmd_wizard(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
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
    dub = _ask("6. Dub mode (captions-only is the only Phase 1 option)", "captions-only")
    transcript = _ask("7. Transcript (auto / path to .srt|.json)", "auto")
    caps = _ask("8. Burn captions? (yes/no)", "yes").lower().startswith("y")
    fill = _ask("   Reframe fill (crop / blur)", "crop")
    output = _ask("12. Output directory", cfg.get("paths.output_dir", "out"))

    cfg.override("reframe.aspect", aspect)
    cfg.override("reframe.fill", fill)
    cfg.override("select.target_duration", _to_int(duration, 45))
    cfg.override("select.num_clips", _to_int(num, 0))
    cfg.override("captions.enabled", caps)
    cfg.override("paths.output_dir", output)
    if dub and not dub.lower().startswith("caption"):
        log.info("Dubbing is Phase 3; Phase 1 produces captions-only clips.")

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
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Run the pipeline non-interactively")
    r.add_argument("source", help="Your video: a URL or a local file path")
    r.add_argument("--owner-confirmed", action="store_true",
                   help="Confirm the source is your own / licensed content (required)")
    r.add_argument("--duration", type=int, help="Target clip duration in seconds")
    r.add_argument("--tolerance", type=int, help="Allowed +/- seconds from target")
    r.add_argument("--num-clips", type=int, help="Number of clips (0 = auto-recommend)")
    r.add_argument("--aspect", help="9:16 / 1:1 / 16:9 / WxH (e.g. 1080x1920)")
    r.add_argument("--fill", choices=["crop", "blur"], help="Reframe fill mode")
    r.add_argument("--no-captions", action="store_true", help="Do not burn captions")
    r.add_argument("--whisper-model", help="tiny|base|small|medium|large-v3")
    r.add_argument("--transcript", help="Use a supplied .srt/.json instead of ASR")
    r.add_argument("--use-llm", dest="backend", action="store_const", const="llm",
                   help="Force Claude hook detection (needs ANTHROPIC_API_KEY)")
    r.add_argument("--no-llm", dest="backend", action="store_const", const="heuristic",
                   help="Force the keyword heuristic")
    r.add_argument("--work-dir", help="Cache/intermediate directory")
    r.add_argument("--output", help="Output directory for rendered clips")
    r.set_defaults(func=cmd_run)

    w = sub.add_parser("wizard", help="Interactive operator wizard")
    w.set_defaults(func=cmd_wizard)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
