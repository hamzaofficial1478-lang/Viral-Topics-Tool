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

import json
import os
from dataclasses import dataclass, asdict

from ..config import Config
from ..models import Clip
from ..utils import log
from .crop import compute_crop
from .track import Track, ensure_model

_DETECTOR_VERSION = "vcam-1"


@dataclass
class Sample:
    t: float
    x: float | None       # salient center x in source px (None = nothing found)
    y: float | None
    cut: bool             # a scene cut occurs at/just before this sample
    signal: str           # "speaker" | "motion" | "none"


def _params(cfg: Config, src_w: int) -> dict:
    return {
        "deadzone": float(cfg.get("reframe.deadzone_pct", 8)) / 100.0 * src_w,
        "snap": float(cfg.get("reframe.snap_threshold_pct", 45)) / 100.0 * src_w,
        "max_pan": float(cfg.get("reframe.max_pan_speed_pct_s", 35)) / 100.0 * src_w,
        "min_dwell": float(cfg.get("reframe.min_dwell_s", 1.2)),
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
    hz = cfg.get("reframe.sample_hz", 6)
    return f"vcam_{clip.clip_id}_{hz}_{_DETECTOR_VERSION}.json"


def detect_saliency(source_path: str, clip: Clip, cfg: Config, cache=None) -> list[Sample] | None:
    """Per-sample saliency + scene cuts for ``clip`` (cached). None if no cv2."""
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

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        return None
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if src_w <= 0 or src_h <= 0:
        cap.release()
        return None

    # A4.9: detect on downscaled frames (~480p), scale coords back up.
    scale = min(1.0, 480.0 / src_h) if src_h > 480 else 1.0
    dw, dh = int(src_w * scale), int(src_h * scale)
    score_thr = float(cfg.get("reframe.face_score", 0.6))
    detector = cv2.FaceDetectorYN.create(model, "", (dw, dh), score_thr)
    detector.setInputSize((dw, dh))

    hz = float(cfg.get("reframe.sample_hz", 6))
    interval = 1.0 / max(1.0, hz)
    cut_thr = float(cfg.get("reframe.scene_cut_threshold", 0.18))
    redetect = bool(cfg.get("reframe.redetect_on_scene_cut", True))
    # motion is salient when the CHANGED AREA is big enough (works for small
    # moving objects, unlike a whole-frame mean-diff threshold).
    motion_min_area = float(cfg.get("reframe.motion_min_area_pct", 0.3)) / 100.0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if fps <= 0:
        fps = 30.0

    samples: list[Sample] = []
    prev_gray = None
    t = 0.0
    while t <= clip.duration + 1e-3:
        # Seek by frame index (POS_MSEC is unreliable on some builds).
        cap.set(cv2.CAP_PROP_POS_FRAMES, int((clip.start + t) * fps))
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        small = cv2.resize(frame, (dw, dh)) if scale < 1.0 else frame
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
        _, faces = detector.detect(small)
        if faces is not None and len(faces) > 0:
            f = max(faces, key=lambda r: float(r[2]) * float(r[3]))
            cx = float(f[0] + f[2] / 2.0) / scale
            cy = float(f[1] + f[3] / 2.0) / scale
            signal = "speaker"
        elif motion_c is not None and motion_area >= motion_min_area and not cut:
            cx, cy = motion_c
            signal = "motion"

        samples.append(Sample(t=round(t, 3), x=cx, y=cy, cut=cut, signal=signal))
        t += interval
    cap.release()

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
    hz = float(cfg.get("reframe.sample_hz", 6))
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
