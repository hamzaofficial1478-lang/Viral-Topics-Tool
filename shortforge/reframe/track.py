"""M5 (Phase 2) — subject tracking for the vertical reframe.

Detects the main speaker with OpenCV's YuNet face detector on sampled frames,
builds a smoothed pan path (crop centre over time), and exposes it so the render
step can crop each frame to keep the speaker in frame. Everything degrades
gracefully: if OpenCV or the model is unavailable, or no face is found, the
caller falls back to the Phase-1 center-crop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import Config
from ..models import Clip
from ..utils import log
from .crop import compute_crop

_MODEL_NAME = "face_detection_yunet_2023mar.onnx"
_MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
_MODEL_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
    "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
)


@dataclass
class Track:
    """A smoothed crop path. Centres are in source pixels."""

    cw: int
    ch: int
    times: list[float]      # sample timestamps, relative to clip start
    cx: list[float]
    cy: list[float]
    src_w: int
    src_h: int

    def center_at(self, t: float) -> tuple[float, float]:
        """Linearly interpolate the crop centre at clip-relative time ``t``."""
        times = self.times
        if not times:
            return self.src_w / 2.0, self.src_h / 2.0
        if t <= times[0]:
            return self.cx[0], self.cy[0]
        if t >= times[-1]:
            return self.cx[-1], self.cy[-1]
        # Binary-ish linear scan (sample counts are small).
        for i in range(1, len(times)):
            if t <= times[i]:
                t0, t1 = times[i - 1], times[i]
                f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return (
                    self.cx[i - 1] + f * (self.cx[i] - self.cx[i - 1]),
                    self.cy[i - 1] + f * (self.cy[i] - self.cy[i - 1]),
                )
        return self.cx[-1], self.cy[-1]

    def topleft_at(self, t: float) -> tuple[int, int]:
        """Clamped crop top-left (x, y) at time ``t``."""
        cx, cy = self.center_at(t)
        x = int(round(cx - self.cw / 2.0))
        y = int(round(cy - self.ch / 2.0))
        x = max(0, min(x, self.src_w - self.cw))
        y = max(0, min(y, self.src_h - self.ch))
        return x, y


def model_path() -> str | None:
    p = os.path.join(_MODEL_DIR, _MODEL_NAME)
    if os.path.isfile(p) and os.path.getsize(p) > 100_000:
        return p
    return None


def ensure_model() -> str | None:
    """Return the model path, downloading it once if missing."""
    p = model_path()
    if p:
        return p
    try:
        import urllib.request

        os.makedirs(_MODEL_DIR, exist_ok=True)
        dest = os.path.join(_MODEL_DIR, _MODEL_NAME)
        log.info("downloading YuNet face model ...")
        urllib.request.urlretrieve(_MODEL_URL, dest)
        return model_path()
    except Exception as e:  # noqa: BLE001 - offline is fine, we just fall back
        log.warning("could not fetch face model (%s); center-crop fallback", e)
        return None


def available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return ensure_model() is not None


def _pick_face(faces):
    """Pick the largest detected face (assumed main speaker)."""
    best = None
    best_area = 0.0
    for f in faces:
        w, h = float(f[2]), float(f[3])
        area = w * h
        if area > best_area:
            best_area = area
            best = f
    return best


def _smooth(values: list[float], window: int) -> list[float]:
    if window <= 1 or len(values) <= 2:
        return values
    out: list[float] = []
    half = window // 2
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _fill_gaps(samples: list[tuple[float, float] | None]) -> list[tuple[float, float]] | None:
    """Interpolate/hold across frames with no detection. None if all missing."""
    known = [(i, v) for i, v in enumerate(samples) if v is not None]
    if not known:
        return None
    out: list[tuple[float, float]] = [None] * len(samples)  # type: ignore
    # Forward/backward hold + linear interp between known indices.
    for k in range(len(known)):
        idx, val = known[k]
        out[idx] = val
    # edges
    first_i, first_v = known[0]
    last_i, last_v = known[-1]
    for i in range(0, first_i):
        out[i] = first_v
    for i in range(last_i + 1, len(samples)):
        out[i] = last_v
    # gaps between
    for k in range(len(known) - 1):
        i0, v0 = known[k]
        i1, v1 = known[k + 1]
        for i in range(i0 + 1, i1):
            f = (i - i0) / (i1 - i0)
            out[i] = (v0[0] + f * (v1[0] - v0[0]), v0[1] + f * (v1[1] - v0[1]))
    return out  # type: ignore


def plan_track(
    source_path: str,
    clip: Clip,
    out_w: int,
    out_h: int,
    cfg: Config,
) -> Track | None:
    """Plan a smoothed crop path following the main speaker across ``clip``.

    Returns None (caller uses center-crop) if OpenCV/model are unavailable or no
    face is detected anywhere in the clip.
    """
    try:
        import cv2
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

    cw, ch, _, _ = compute_crop(src_w, src_h, out_w, out_h)

    interval = float(cfg.get("reframe.track_sample_interval", 0.33))
    score_thr = float(cfg.get("reframe.face_score", 0.6))
    detector = cv2.FaceDetectorYN.create(model, "", (src_w, src_h), score_thr)
    detector.setInputSize((src_w, src_h))

    times: list[float] = []
    raw: list[tuple[float, float] | None] = []
    t = 0.0
    duration = clip.duration
    while t <= duration + 1e-3:
        cap.set(cv2.CAP_PROP_POS_MSEC, (clip.start + t) * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        _, faces = detector.detect(frame)
        center = None
        if faces is not None and len(faces) > 0:
            f = _pick_face(faces)
            if f is not None:
                center = (float(f[0] + f[2] / 2.0), float(f[1] + f[3] / 2.0))
        times.append(t)
        raw.append(center)
        t += interval
    cap.release()

    if not times:
        return None
    filled = _fill_gaps(raw)
    if filled is None:
        log.info("clip %s: no face detected, using center-crop", clip.clip_id)
        return None

    detected = sum(1 for v in raw if v is not None)
    log.info(
        "clip %s: tracked speaker in %d/%d sampled frames",
        clip.clip_id, detected, len(raw),
    )

    win = int(cfg.get("reframe.track_smooth_window", 5))
    cx = _smooth([v[0] for v in filled], win)
    cy = _smooth([v[1] for v in filled], win)
    return Track(cw=cw, ch=ch, times=times, cx=cx, cy=cy, src_w=src_w, src_h=src_h)
