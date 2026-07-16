"""M13 — Render one clip: cut + reframe + burn captions + export.

A single ffmpeg pass. ``-ss`` before ``-i`` gives a fast, frame-accurate seek
(ffmpeg decodes to the exact frame when re-encoding); ``-t`` bounds the length.
The video pad ``[v]`` comes from the reframe/caption filtergraph; audio is taken
straight from the source (also seeked, so it stays in sync).
"""

from __future__ import annotations

import os

from ..config import Config
from ..models import Clip
from ..utils import require_binary, run, format_timestamp, ffprobe_info, log


def render_clip(
    source_path: str,
    clip: Clip,
    filtergraph: str,
    cfg: Config,
    out_path: str,
) -> str:
    """Encode ``clip`` to ``out_path`` (H.264/AAC mp4). Returns the path."""
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    crf = str(cfg.get("render.crf", 20))
    preset = str(cfg.get("render.preset", "veryfast"))
    abr = str(cfg.get("render.audio_bitrate", "128k"))
    fps = cfg.get("render.fps")

    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        format_timestamp(clip.start),
        "-i",
        source_path,
        "-t",
        f"{clip.duration:.3f}",
        "-filter_complex",
        filtergraph,
        "-map",
        "[v]",
        "-map",
        "0:a:0?",  # optional: sources without audio still render
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        abr,
        "-movflags",
        "+faststart",
    ]
    if fps:
        cmd += ["-r", str(fps)]
    cmd.append(out_path)

    log.info("rendering clip %s -> %s", clip.clip_id, out_path)
    run(cmd)

    info = ffprobe_info(out_path)
    log.info(
        "  clip %s: %dx%d, %.1fs%s",
        clip.clip_id,
        info.width,
        info.height,
        info.duration,
        "" if info.has_audio else " (no audio)",
    )
    return out_path
