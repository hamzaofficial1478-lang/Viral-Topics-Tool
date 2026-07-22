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


def _threads_args(cfg: Config) -> list[str]:
    """STEP 5: per-ffmpeg ``-threads`` so parallel clip renders don't oversubscribe
    the CPU. 0 (auto) lets ffmpeg decide; the pipeline sets this when it fans out."""
    n = int(cfg.get("render.threads", 0) or 0)
    return ["-threads", str(n)] if n > 0 else []


# STEP 1: hardware H.264 encoders, in preference order. QSV = Intel Quick Sync.
_HW_ENCODERS = ("h264_qsv", "h264_nvenc", "h264_amf")
_hw_cache: list[str] | None = None


def available_hw_encoders() -> list[str]:
    """Hardware H.264 encoders this ffmpeg build exposes (cached). Detected via
    ``ffmpeg -encoders`` at startup; recorded in ``doctor``."""
    global _hw_cache
    if _hw_cache is not None:
        return _hw_cache
    try:
        out = subprocess.run([require_binary("ffmpeg"), "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        out = ""
    _hw_cache = [e for e in _HW_ENCODERS if e in out]
    return _hw_cache


def resolve_encoder(cfg: Config) -> str:
    """The ffmpeg video encoder to use. ``render.encoder``: auto | qsv | nvenc |
    amf | x264. 'auto' picks the best available hardware encoder, else libx264;
    an explicit hardware choice that isn't available falls back to libx264."""
    choice = str(cfg.get("render.encoder", "x264") or "x264").lower()
    if choice in ("x264", "libx264", "software", "cpu"):
        return "libx264"   # default path: no `ffmpeg -encoders` probe needed
    hw = available_hw_encoders()
    if choice == "auto":
        return hw[0] if hw else "libx264"
    name = choice if choice.startswith("h264_") else f"h264_{choice}"
    if name in hw:
        return name
    log.warning("encoder '%s' not available (have: %s) — using libx264",
                choice, ", ".join(hw) or "none")
    return "libx264"


def video_encode_args(cfg: Config, encoder: str | None = None) -> list[str]:
    """``-c:v …`` args for the chosen encoder. Hardware encoders are tuned for
    quality-equivalence to the x264 CRF (raise the bitrate/quality knob rather
    than accept visible loss); software x264 keeps CRF + preset + -threads."""
    enc = encoder or resolve_encoder(cfg)
    if enc == "h264_qsv":
        gq = str(cfg.get("render.qsv_quality", 23))   # like CRF: lower = better
        return ["-c:v", "h264_qsv", "-global_quality", gq, "-pix_fmt", "nv12"]
    if enc == "h264_nvenc":
        cq = str(cfg.get("render.nvenc_cq", 23))
        return ["-c:v", "h264_nvenc", "-rc", "vbr", "-cq", cq, "-pix_fmt", "yuv420p"]
    if enc == "h264_amf":
        qp = str(cfg.get("render.amf_qp", 22))
        return ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", qp, "-qp_p", qp, "-pix_fmt", "yuv420p"]
    # libx264 (software, default)
    return (["-c:v", "libx264", "-preset", str(cfg.get("render.preset", "veryfast")),
             "-crf", str(cfg.get("render.crf", 20)), "-pix_fmt", "yuv420p"]
            + _threads_args(cfg))


def plan_render(cfg: Config, n_clips: int) -> tuple[int, int]:
    """STEP 5: (workers, threads_per_ffmpeg) for rendering ``n_clips`` clips.

    Default workers = ``min(clips, physical_cores // 2)`` (physical ≈ logical//2
    with hyperthreading), so a 2-clip job renders both at once without pinning
    the whole CPU. Per-process ``-threads`` = ``logical // workers`` so the
    concurrent ffmpegs together use the cores once, not N× over. Both overridable
    via ``render.workers`` / ``render.threads``."""
    import os
    logical = os.cpu_count() or 2
    physical = max(1, logical // 2)
    w = int(cfg.get("render.workers", 0) or 0)
    if w <= 0:
        w = max(1, min(int(n_clips), max(1, physical // 2)))
    else:
        w = max(1, min(w, int(n_clips)))
    t = int(cfg.get("render.threads", 0) or 0)
    if t <= 0 and w > 1:
        t = max(1, logical // w)
    return w, t


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

    abr = str(cfg.get("render.audio_bitrate", "128k"))
    fps = cfg.get("render.fps")
    af = loudnorm_filter(cfg)

    pre = [ffmpeg, "-y", "-ss", format_timestamp(clip.start), "-i", source_path]
    fc = filtergraph
    extra_af: str | None = af
    if audio_path:
        pre += ["-i", audio_path]
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

    def _cmd(enc: str) -> list[str]:
        c = list(pre) + [
            "-t", f"{clip.duration:.3f}",
            "-filter_complex", fc,
            "-map", "[v]", "-map", audio_map,
        ] + video_encode_args(cfg, enc)
        if extra_af:
            c += ["-af", extra_af]
        # A3: -sn drops any soft subtitle stream so only our caption layer exists.
        c += ["-c:a", "aac", "-b:a", abr, "-sn", "-movflags", "+faststart", "-shortest"]
        if fps:
            c += ["-r", str(fps)]
        c.append(out_path)
        return c

    encoder = resolve_encoder(cfg)
    log.info("rendering clip %s -> %s (%s)", clip.clip_id, out_path, encoder)
    try:
        run(_cmd(encoder))
    except ShortForgeError:
        if encoder != "libx264":   # STEP 1: auto-fall back to software on hw failure
            log.warning("hardware encoder %s failed for clip %s; retrying with libx264",
                        encoder, clip.clip_id)
            run(_cmd("libx264"))
        else:
            raise

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
    _encoder: str | None = None,
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

    abr = str(cfg.get("render.audio_bitrate", "128k"))
    af = loudnorm_filter(cfg)
    encoder = _encoder or resolve_encoder(cfg)

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
    ] + video_encode_args(cfg, encoder)
    if extra_af:
        cmd += ["-af", extra_af]
    cmd += [
        "-c:a", "aac", "-b:a", abr, "-sn", "-movflags", "+faststart",
        "-shortest", out_path,
    ]

    import tempfile

    log.info("rendering clip %s (tracked) -> %s (%s)", clip.clip_id, out_path, encoder)
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
        if encoder != "libx264":   # STEP 1: hardware encode failed — retry on x264
            log.warning("hardware encoder %s failed for clip %s (tracked); retrying libx264",
                        encoder, clip.clip_id)
            return render_clip_tracked(
                source_path, clip, track, out_w, out_h, filtergraph, cfg, out_path,
                audio_path=audio_path, keep_ranges=keep_ranges,
                burned_band=burned_band, burned_mode=burned_mode, _encoder="libx264")
        raise ShortForgeError("tracked render failed:\n" + "\n".join(tail))
    errf.close()

    info = ffprobe_info(out_path)
    log.info("  clip %s: %dx%d, %.1fs (tracked, %d frames)",
             clip.clip_id, info.width, info.height, info.duration, written)
    return out_path
