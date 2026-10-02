"""A4 — Virtual camera: a per-frame crop path that follows the action.

The old tracker locked onto the largest face at the start and smoothed one path
across the whole clip, so it stayed on the wrong region when the shot cut or the
subject moved. This replaces it with a virtual camera:

1. Per-sample **saliency** (priority: active-ish speaker face -> dominant motion
   -> center) at a low sample rate on downscaled frames (CPU-friendly).
2. **Scene-cut reset (highest-value fix):** at every detected cut the camera
   discards its state and re-targets from scratch — never carry a box across a cut.
3. A **hold / pan / snap** state machine with a deadzone (ignore small jitter),
   a velocity cap (smooth pans), a minimum dwell (don't oscillate in dialogue),
   and a large-displacement **snap** (hard cut between subjects, not a long sweep).

The planner (`plan_path`) is pure and unit-tested; detection (`detect_saliency`)
needs OpenCV + the YuNet model and is cached so re-renders never recompute it.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from ..config import Config
from ..models import Clip
from ..utils import log
from .crop import compute_crop
from .track import Track, ensure_model

_DETECTOR_VERSION = "vcam-3"   # bumped: sticky face tracking (was: biggest face per sample)


@dataclass
class Sample:
    t: float
    x: float | None       # salient center x in source px (None = nothing found)
    y: float | None
    cut: bool             # a scene cut occurs at/just before this sample
    signal: str           # "speaker" | "motion" | "none"


def _params(cfg: Config, src_w: int) -> dict:
    return {
        "deadzone": float(cfg.get("reframe.deadzone_pct", 12)) / 100.0 * src_w,
        "snap": float(cfg.get("reframe.snap_threshold_pct", 60)) / 100.0 * src_w,
        "max_pan": float(cfg.get("reframe.max_pan_speed_pct_s", 22)) / 100.0 * src_w,
        "min_dwell": float(cfg.get("reframe.min_dwell_s", 2.0)),
    }


def plan_path(
    centers: list[tuple[float, float] | None],
    cuts: list[bool],
    times: list[float],
    frame_center: tuple[float, float],
    params: dict,
) -> list[tuple[float, float]]:
    """Pure virtual-camera planner. Returns a camera center per sample.

    ``centers[i]`` is the salient target (or None = hold), ``cuts[i]`` marks a
    scene cut (reset), ``times[i]`` are timestamps. See module docstring.
    """
    n = len(centers)
    if n == 0:
        return []
    deadzone = params["deadzone"]
    snap = params["snap"]
    max_pan = params["max_pan"]
    min_dwell = params["min_dwell"]

    fx, fy = frame_center
    cam = centers[0] if centers[0] is not None else (fx, fy)
    out: list[tuple[float, float]] = []
    dwell = 0.0  # time since the last committed re-target

    for i in range(n):
        dt = (times[i] - times[i - 1]) if i > 0 else 0.0
        dwell += dt

        # A4.2: scene cut -> discard state, re-target immediately.
        if cuts[i]:
            cam = centers[i] if centers[i] is not None else (fx, fy)
            dwell = 0.0
            out.append(cam)
            continue

        target = centers[i]
        if target is None:            # nothing salient -> hold
            out.append(cam)
            continue

        dx = target[0] - cam[0]
        dy = target[1] - cam[1]
        dist = (dx * dx + dy * dy) ** 0.5

        if dist < deadzone:           # A4.4 deadzone: ignore small jitter
            out.append(cam)
            continue

        if dist > snap and dwell >= min_dwell:
            # A4.3 snap: different subject, big jump -> hard cut, not a sweep.
            cam = target
            dwell = 0.0
            out.append(cam)
            continue

        # A4.3/4.4 pan: ease toward the target, capped by max pan speed.
        step = max_pan * dt if dt > 0 else dist
        if step <= 0:
            step = dist * 0.5
        if dist <= step:
            cam = target
        else:
            f = step / dist
            cam = (cam[0] + dx * f, cam[1] + dy * f)
        out.append(cam)

    return out


def _clamp_center(cx: float, cy: float, cw: int, ch: int, src_w: int, src_h: int):
    """Keep the crop window fully inside the frame (never black edges)."""
    half_w, half_h = cw / 2.0, ch / 2.0
    cx = max(half_w, min(cx, src_w - half_w))
    cy = max(half_h, min(cy, src_h - half_h))
    return cx, cy


def _cache_key(clip: Clip, cfg: Config) -> str:
    hz = cfg.get("reframe.sample_hz", 4)
    return f"vcam_{clip.clip_id}_{hz}_{_DETECTOR_VERSION}.json"


def _pick_face(faces, prev: tuple[float, float] | None, switch_ratio: float):
    """Choose which face the camera should follow — with temporal STICKINESS.

    Picking the biggest face independently per sample makes the camera flip
    between two similarly-sized people on tiny detection noise, which reads as
    shaky//nervous footage. Instead: stay on whichever face is nearest the one we
    were already following, and only switch to a different person when they are
    clearly more prominent (``switch_ratio``x the area) or the tracked face is
    gone. ``prev`` and the result are (cx, cy) in detection pixels.
    """
    boxes = [(float(f[0]), float(f[1]), float(f[2]), float(f[3])) for f in faces]
    if not boxes:
        return None
    centers = [(x + w / 2.0, y + h / 2.0, w * h) for x, y, w, h in boxes]
    biggest = max(centers, key=lambda c: c[2])
    if prev is None:
        return biggest[0], biggest[1]
    # Nearest to where the camera already is (the face we were following).
    nearest = min(centers, key=lambda c: (c[0] - prev[0]) ** 2 + (c[1] - prev[1]) ** 2)
    # Only abandon it for someone clearly more prominent.
    if biggest[2] >= nearest[2] * switch_ratio:
        return biggest[0], biggest[1]
    return nearest[0], nearest[1]


def _read_exact(stream, n: int) -> bytes | None:
    """Read exactly ``n`` bytes from a pipe (a read may return a partial chunk);
    None at end of stream."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def detect_saliency(source_path: str, clip: Clip, cfg: Config, cache=None) -> list[Sample] | None:
    """Per-sample saliency + scene cuts for ``clip`` (cached). None if no cv2.

    PERF (was the pipeline's #1 bottleneck — ~3s/sample on 1080p): the clip span
    is decoded **once** by ffmpeg, sampled at ``sample_hz`` and scaled to ~480p in
    the *same* pass, then streamed frame-by-frame to the detector. The old path
    called ``cap.set(CAP_PROP_POS_FRAMES)`` per sample, which re-seeks to the
    nearest keyframe and re-decodes forward every time — O(samples × GOP) decode
    work. One linear decode replaces hundreds of random seeks.
    """
    if cache is not None:
        cached = cache.load_json(_cache_key(clip, cfg))
        if cached:
            log.info("clip %s: reusing cached saliency track", clip.clip_id)
            return [Sample(**s) for s in cached]
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    model = ensure_model()
    if not model:
        return None

    import subprocess
    import time
    from ..utils import require_binary, ffprobe_info
    from .. import timing

    # Source dimensions via a cheap probe (no decode).
    try:
        info = ffprobe_info(source_path)
        src_w, src_h = int(info.width), int(info.height)
    except Exception:  # noqa: BLE001
        return None
    if src_w <= 0 or src_h <= 0:
        return None

    # A4.9: detect on downscaled frames (~480p) — scale is applied by ffmpeg in
    # the decode pass, so full-res frames are never materialised. Coords scale up.
    scale = min(1.0, 480.0 / src_h) if src_h > 480 else 1.0
    dw, dh = max(2, int(src_w * scale)), max(2, int(src_h * scale))
    score_thr = float(cfg.get("reframe.face_score", 0.6))
    detector = cv2.FaceDetectorYN.create(model, "", (dw, dh), score_thr)
    detector.setInputSize((dw, dh))

    hz = float(cfg.get("reframe.sample_hz", 4))
    interval = 1.0 / max(1.0, hz)
    cut_thr = float(cfg.get("reframe.scene_cut_threshold", 0.18))
    redetect = bool(cfg.get("reframe.redetect_on_scene_cut", True))
    # motion is salient when the CHANGED AREA is big enough (works for small
    # moving objects, unlike a whole-frame mean-diff threshold).
    motion_min_area = float(cfg.get("reframe.motion_min_area_pct", 0.8)) / 100.0

    # One ffmpeg pass: seek to the clip, sample at `hz`, scale to detection size.
    ffmpeg = require_binary("ffmpeg")
    cmd = [ffmpeg, "-nostdin", "-ss", f"{clip.start:.3f}", "-t", f"{clip.duration:.3f}",
           "-i", source_path, "-an", "-vf", f"fps={hz},scale={dw}:{dh}",
           "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    log.info("saliency: detecting on %dx%d frames (source %dx%d, scale %.3f) at %.1f Hz "
             "in one decode pass", dw, dh, src_w, src_h, scale, hz)
    frame_bytes = dw * dh * 3
    _t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    # Follow one person steadily instead of re-picking the biggest face every
    # sample (that flip-flopping is what made the output look shaky).
    switch_ratio = float(cfg.get("reframe.face_switch_ratio", 1.4))
    prev_face: tuple[float, float] | None = None

    samples: list[Sample] = []
    prev_gray = None
    det_secs = 0.0
    t = 0.0
    try:
        while True:
            buf = _read_exact(proc.stdout, frame_bytes)
            if buf is None:
                break
            small = np.frombuffer(buf, np.uint8).reshape(dh, dw, 3)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

            # scene cut = big mean frame difference vs the previous sample
            cut = False
            motion_c = None
            motion_area = 0.0
            if prev_gray is not None:
                diff = cv2.absdiff(gray, prev_gray)
                cut = redetect and (float(diff.mean()) / 255.0) > cut_thr
                _, thr = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
                motion_area = cv2.countNonZero(thr) / float(dw * dh)
                m = cv2.moments(thr, binaryImage=True)
                if m["m00"] > 0:
                    motion_c = (m["m10"] / m["m00"] / scale, m["m01"] / m["m00"] / scale)
            prev_gray = gray

            # salience priority: face (speaker) -> motion -> none
            cx = cy = None
            signal = "none"
            _d0 = time.time()
            _, faces = detector.detect(small)
            det_secs += time.time() - _d0
            if cut:
                prev_face = None          # new shot: no one to stay locked onto
            picked = _pick_face(faces, prev_face, switch_ratio) if faces is not None else None
            if picked is not None:
                prev_face = picked        # follow this person until someone clearly wins
                cx, cy = picked[0] / scale, picked[1] / scale
                signal = "speaker"
            elif motion_c is not None and motion_area >= motion_min_area and not cut:
                cx, cy = motion_c
                signal = "motion"

            samples.append(Sample(t=round(t, 3), x=cx, y=cy, cut=cut, signal=signal))
            t += interval
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()

    total = time.time() - _t0
    timing.record_ffmpeg(cmd, total)
    n = len(samples)
    if n:
        log.info("saliency: %d samples in %.1fs (detector %.0f ms/frame, %.0f%% of the pass)",
                 n, total, 1000.0 * det_secs / n, 100.0 * det_secs / max(total, 1e-6))

    if cache is not None and samples:
        cache.save_json(_cache_key(clip, cfg), [asdict(s) for s in samples])
    return samples or None


def plan_vcam(source_path: str, clip: Clip, out_w: int, out_h: int,
              cfg: Config, cache=None) -> Track | None:
    """Build a virtual-camera Track for ``clip`` (None -> caller center-crops)."""
    samples = detect_saliency(source_path, clip, cfg, cache)
    if not samples:
        return None
    # Need at least one real detection to be worth tracking.
    if not any(s.x is not None for s in samples):
        log.info("clip %s: no salient subject found, using center-crop", clip.clip_id)
        return None

    import cv2
    cap = cv2.VideoCapture(source_path)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    cw, ch, _, _ = compute_crop(src_w, src_h, out_w, out_h)

    times = [s.t for s in samples]
    centers = [(s.x, s.y) if s.x is not None else None for s in samples]
    cuts = [s.cut for s in samples]
    params = _params(cfg, src_w)
    path = plan_path(centers, cuts, times, (src_w / 2.0, src_h / 2.0), params)

    cx, cy = [], []
    for px, py in path:
        ccx, ccy = _clamp_center(px, py, cw, ch, src_w, src_h)
        cx.append(ccx)
        cy.append(ccy)

    cuts_n = sum(1 for c in cuts if c)
    spk = sum(1 for s in samples if s.signal == "speaker")
    mot = sum(1 for s in samples if s.signal == "motion")
    log.info("clip %s: virtual camera over %d samples (%d speaker, %d motion, %d cuts)",
             clip.clip_id, len(samples), spk, mot, cuts_n)
    return Track(cw=cw, ch=ch, times=times, cx=cx, cy=cy, src_w=src_w, src_h=src_h)


def debug_reframe(source_path: str, clip: Clip, out_w: int, out_h: int,
                  cfg: Config, cache, out_path: str) -> str | None:
    """A4.7 diagnostic: source-aspect video showing the saliency point, the
    crop window, scene-cut markers, and the signal driving each frame."""
    try:
        import cv2
    except ImportError:
        return None
    samples = detect_saliency(source_path, clip, cfg, cache)
    track = plan_vcam(source_path, clip, out_w, out_h, cfg, cache)
    if not samples or track is None:
        return None

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        return None
    hz = float(cfg.get("reframe.sample_hz", 4))
    src_w, src_h = track.src_w, track.src_h
    scale = min(1.0, 720.0 / src_w)
    dw, dh = int(src_w * scale), int(src_h * scale)
    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), hz, (dw, dh))
    colors = {"speaker": (0, 255, 0), "motion": (0, 165, 255), "none": (128, 128, 128)}
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if fps <= 0:
        fps = 30.0
    for s in samples:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int((clip.start + s.t) * fps))
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        frame = cv2.resize(frame, (dw, dh))
        x, y = track.topleft_at(s.t)
        cv2.rectangle(frame, (int(x * scale), int(y * scale)),
                      (int((x + track.cw) * scale), int((y + track.ch) * scale)),
                      (255, 255, 255), 2)
        if s.x is not None:
            cv2.circle(frame, (int(s.x * scale), int(s.y * scale)), 8,
                       colors.get(s.signal, (128, 128, 128)), -1)
        cv2.putText(frame, s.signal, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    colors.get(s.signal, (200, 200, 200)), 2)
        if s.cut:
            cv2.putText(frame, "SCENE CUT", (8, dh - 16), cv2.FONT_HERSHEY_SIMPLEX,
                        0.9, (0, 0, 255), 3)
        writer.write(frame)
    writer.release()
    cap.release()
    log.info("clip %s: wrote reframe debug video -> %s", clip.clip_id, out_path)
    return out_path
