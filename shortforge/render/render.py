"""M13 — Render one clip: cut + reframe + burn captions + export.

A single ffmpeg pass. ``-ss`` before ``-i`` gives a fast, frame-accurate seek
(ffmpeg decodes to the exact frame when re-encoding); ``-t`` bounds the length.
The video pad ``[v]`` comes from the reframe/caption filtergraph; audio is taken
straight from the source (also seeked, so it stays in sync).
"""

from __future__ import annotations

import os
import subprocess
import time

from ..analyze.audio import loudnorm_filter, select_expr
from ..config import Config
from ..models import Clip
from ..utils import ShortForgeError, require_binary, run, format_timestamp, ffprobe_info, log


def _in_ranges(t: float, ranges: list[tuple[float, float]]) -> bool:
    """True if instant ``t`` falls inside any (start, end) range."""
    return any(s <= t <= e for s, e in ranges)


def render_clip(
    source_path: str,
    clip: Clip,
    filtergraph: str,
    cfg: Config,
    out_path: str,
    audio_path: str | None = None,
    keep_ranges: list[tuple[float, float]] | None = None,
) -> str:
    """Encode ``clip`` to ``out_path`` (H.264/AAC mp4). Returns the path.

    ``audio_path`` (Phase 3) supplies an external dubbed audio track (already
    clip-length); otherwise audio is taken from the seeked source.
    ``keep_ranges`` (M9 jump cuts, source-time) trims dead air from the source
    audio to match the video select already baked into ``filtergraph``.
    """
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    crf = str(cfg.get("render.crf", 20))
    preset = str(cfg.get("render.preset", "veryfast"))
    abr = str(cfg.get("render.audio_bitrate", "128k"))
    fps = cfg.get("render.fps")
    af = loudnorm_filter(cfg)

    cmd = [ffmpeg, "-y", "-ss", format_timestamp(clip.start), "-i", source_path]
    fc = filtergraph
    extra_af: str | None = af
    if audio_path:
        cmd += ["-i", audio_path]
        audio_map = "1:a:0"
    elif keep_ranges:
        # Cut the source audio on the same ranges as the video, then close the
        # gaps (asetpts). loudnorm folds into the same chain; -af is not used.
        expr = select_expr(keep_ranges, clip.start)
        achain = f"[0:a]aselect='{expr}',asetpts=N/SR/TB"
        if af:
            achain += f",{af}"
        fc = fc + ";" + achain + "[a]"
        audio_map = "[a]"
        extra_af = None
    else:
        audio_map = "0:a:0?"  # optional: sources without audio still render
    cmd += [
        "-t", f"{clip.duration:.3f}",
        "-filter_complex", fc,
        "-map", "[v]", "-map", audio_map,
        "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
    ]
    if extra_af:
        cmd += ["-af", extra_af]
    # A3: -sn drops any soft subtitle stream so only our caption layer exists.
    cmd += ["-c:a", "aac", "-b:a", abr, "-sn", "-movflags", "+faststart", "-shortest"]
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
    audio_path: str | None = None,
    keep_ranges: list[tuple[float, float]] | None = None,
    burned_band: dict | None = None,
    burned_mode: str = "cover",
) -> str:
    """Render a clip with per-frame subject-tracking crop (M5, Phase 2).

    OpenCV reads the clip's frames, crops each around the smoothed speaker
    position, and pipes them to ffmpeg (input 0). Audio comes from the seeked
    source (input 1); ``filtergraph`` burns captions/logo onto the pre-cropped
    video and ends in ``[v]``. ``keep_ranges`` (M9 jump cuts, source-time) drop
    dead-air frames here and trim the source audio to match.
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
    af = loudnorm_filter(cfg)

    # Clip-relative keep ranges for the per-frame test (source -> clip time).
    rel_keep = (
        [(max(0.0, s - clip.start), max(0.0, e - clip.start)) for s, e in keep_ranges]
        if keep_ranges else None
    )

    cmd = [
        ffmpeg, "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{out_w}x{out_h}", "-r", f"{fps}", "-i", "pipe:0",
    ]
    fc = filtergraph
    extra_af: str | None = af
    if audio_path:
        cmd += ["-i", audio_path]  # dubbed audio (already clip-length)
        audio_map = "1:a:0?"
    else:
        cmd += ["-ss", format_timestamp(clip.start), "-t", f"{clip.duration:.3f}",
                "-i", source_path]
        if rel_keep:
            expr = select_expr(keep_ranges, clip.start)
            achain = f"[1:a]aselect='{expr}',asetpts=N/SR/TB"
            if af:
                achain += f",{af}"
            fc = fc + ";" + achain + "[a]"
            audio_map = "[a]"
            extra_af = None
        else:
            audio_map = "1:a:0?"
    cmd += [
        "-filter_complex", fc,
        "-map", "[v]", "-map", audio_map,
        "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
    ]
    if extra_af:
        cmd += ["-af", extra_af]
    cmd += [
        "-c:a", "aac", "-b:a", abr, "-sn", "-movflags", "+faststart",
        "-shortest", out_path,
    ]

    import tempfile

    log.info("rendering clip %s (tracked) -> %s", clip.clip_id, out_path)
    # File-backed stderr avoids a pipe-buffer deadlock while we stream frames in.
    errf = tempfile.TemporaryFile()
    _t0 = time.time()
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
            frame_i += 1
            # Jump cuts: drop frames that fall in trimmed dead air.
            if rel_keep is not None and not _in_ranges(t, rel_keep):
                continue
            # A3: treat burned-in source captions before cropping/piping.
            if burned_band is not None:
                from ..captions import burned_in
                frame = burned_in.treat_frame(frame, burned_band, burned_mode)
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
    finally:
        cap.release()
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    proc.wait()
    # STEP 0: record this ffmpeg encode (the tracked path pipes frames via a raw
    # Popen, so it bypasses utils.run's logging — log + record it here too).
    _dt = time.time() - _t0
    log.info("ffmpeg %.1fs: %s", _dt, " ".join(cmd))
    from .. import timing
    timing.record_ffmpeg(cmd, _dt)
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
