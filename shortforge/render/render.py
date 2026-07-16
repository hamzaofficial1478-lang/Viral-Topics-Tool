"""M13 — Render one clip: cut + reframe + burn captions + export.

A single ffmpeg pass. ``-ss`` before ``-i`` gives a fast, frame-accurate seek
(ffmpeg decodes to the exact frame when re-encoding); ``-t`` bounds the length.
The video pad ``[v]`` comes from the reframe/caption filtergraph; audio is taken
straight from the source (also seeked, so it stays in sync).
"""

from __future__ import annotations

import os
import subprocess

from ..analyze.audio import loudnorm_filter
from ..config import Config
from ..models import Clip
from ..utils import ShortForgeError, require_binary, run, format_timestamp, ffprobe_info, log


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
    ]
    af = loudnorm_filter(cfg)
    if af:
        cmd += ["-af", af]
    cmd += ["-c:a", "aac", "-b:a", abr, "-movflags", "+faststart"]
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


def render_clip_tracked(
    source_path: str,
    clip: Clip,
    track,
    out_w: int,
    out_h: int,
    filtergraph: str,
    cfg: Config,
    out_path: str,
) -> str:
    """Render a clip with per-frame subject-tracking crop (M5, Phase 2).

    OpenCV reads the clip's frames, crops each around the smoothed speaker
    position, and pipes them to ffmpeg (input 0). Audio comes from the seeked
    source (input 1); ``filtergraph`` burns captions/logo onto the pre-cropped
    video and ends in ``[v]``.
    """
    import cv2  # available: caller checked track is not None

    ffmpeg = require_binary("ffmpeg")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        raise ShortForgeError(f"OpenCV could not open {source_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if fps <= 0:
        fps = 30.0

    crf = str(cfg.get("render.crf", 20))
    preset = str(cfg.get("render.preset", "veryfast"))
    abr = str(cfg.get("render.audio_bitrate", "128k"))

    cmd = [
        ffmpeg, "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{out_w}x{out_h}", "-r", f"{fps}", "-i", "pipe:0",
        "-ss", format_timestamp(clip.start), "-t", f"{clip.duration:.3f}",
        "-i", source_path,
        "-filter_complex", filtergraph,
        "-map", "[v]", "-map", "1:a:0?",
        "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
    ]
    af = loudnorm_filter(cfg)
    if af:
        cmd += ["-af", af]
    cmd += [
        "-c:a", "aac", "-b:a", abr, "-movflags", "+faststart",
        "-shortest", out_path,
    ]

    import tempfile

    log.info("rendering clip %s (tracked) -> %s", clip.clip_id, out_path)
    # File-backed stderr avoids a pipe-buffer deadlock while we stream frames in.
    errf = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)

    cap.set(cv2.CAP_PROP_POS_MSEC, clip.start * 1000.0)
    frame_i = 0
    written = 0
    broken = False
    try:
        while True:
            t = frame_i / fps
            if t >= clip.duration:
                break
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            x, y = track.topleft_at(t)
            crop = frame[y : y + track.ch, x : x + track.cw]
            if crop.shape[0] != track.ch or crop.shape[1] != track.cw:
                crop = cv2.resize(crop, (track.cw, track.ch))
            resized = cv2.resize(crop, (out_w, out_h), interpolation=cv2.INTER_AREA)
            try:
                proc.stdin.write(resized.tobytes())
            except BrokenPipeError:
                broken = True
                break
            written += 1
            frame_i += 1
    finally:
        cap.release()
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    proc.wait()
    if proc.returncode != 0 or broken:
        errf.seek(0)
        tail = errf.read().decode(errors="replace").strip().splitlines()[-12:]
        errf.close()
        raise ShortForgeError("tracked render failed:\n" + "\n".join(tail))
    errf.close()

    info = ffprobe_info(out_path)
    log.info("  clip %s: %dx%d, %.1fs (tracked, %d frames)",
             clip.clip_id, info.width, info.height, info.duration, written)
    return out_path
