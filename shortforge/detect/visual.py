"""M3++ — local visual hook signals (no API).

The transcript only tells you what was *said*. This module adds what's *shown*:
per-segment motion energy, scene-cut density, and face presence — so visually
strong moments (fast cuts, reactions, b-roll payoffs) get scored too. Signals
are cheap, local, and combine with the transcript score in ``detect_hooks``.

The heavier "Claude actually watches the frames" scorer is in ``vision_llm.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..utils import log


@dataclass
class VisualSignals:
    """Sampled per-frame signals + a windowed scorer."""

    times: list[float]
    motion: list[float]      # 0..1 change energy vs previous sample
    face_area: list[float]   # 0..1 largest-face area fraction
    cut_times: list[float]   # timestamps of detected scene cuts

    def score_window(self, start: float, end: float, cfg: Config) -> tuple[float, dict]:
        sel = [i for i, t in enumerate(self.times) if start <= t < end]
        if not sel:
            return 0.0, {}
        motion_ref = float(cfg.get("detect.visual_motion_ref", 0.12))
        cut_ref = float(cfg.get("detect.visual_cut_ref", 0.5))     # cuts/sec to saturate
        face_ref = float(cfg.get("detect.visual_face_ref", 0.12))  # face frame-fraction
        w_m = float(cfg.get("detect.visual_w_motion", 0.4))
        w_c = float(cfg.get("detect.visual_w_cuts", 0.3))
        w_f = float(cfg.get("detect.visual_w_face", 0.3))

        m = sum(self.motion[i] for i in sel) / len(sel)
        f = sum(self.face_area[i] for i in sel) / len(sel)
        dur = max(0.5, end - start)
        cuts = sum(1 for c in self.cut_times if start <= c < end)
        cut_density = cuts / dur

        mm = min(1.0, m / motion_ref) if motion_ref else 0.0
        cc = min(1.0, cut_density / cut_ref) if cut_ref else 0.0
        ff = min(1.0, f / face_ref) if face_ref else 0.0
        score = max(0.0, min(1.0, w_m * mm + w_c * cc + w_f * ff))
        return score, {
            "motion": round(mm, 3),
            "cut_density": round(cut_density, 3),
            "face": round(ff, 3),
        }


def available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


def analyze(source_path: str, duration: float, cfg: Config) -> VisualSignals | None:
    """Sample the video and compute motion / cut / face signals. None if no cv2."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        return None

    interval = float(cfg.get("detect.visual_sample_interval", 0.5))
    max_samples = int(cfg.get("detect.visual_max_samples", 1500))
    if duration > 0 and duration / interval > max_samples:
        interval = duration / max_samples  # keep long videos bounded
    cut_thr = float(cfg.get("detect.visual_cut_threshold", 0.25))
    proc_w = 320

    # Optional face detector (reuse the Phase-2 YuNet model).
    detector = None
    try:
        from ..reframe.track import ensure_model

        model = ensure_model()
        if model:
            detector = cv2.FaceDetectorYN.create(model, "", (proc_w, proc_w), 0.6)
    except Exception:  # noqa: BLE001
        detector = None

    times: list[float] = []
    motion: list[float] = []
    face_area: list[float] = []
    cut_times: list[float] = []
    prev_gray = None

    t = 0.0
    dur = duration if duration > 0 else 1e9
    while t < dur:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        h, w = frame.shape[:2]
        scale = proc_w / max(1, w)
        small = cv2.resize(frame, (proc_w, max(1, int(h * scale))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if prev_gray is not None and prev_gray.shape == gray.shape:
            d = float(np.mean(np.abs(gray.astype(np.int16) - prev_gray.astype(np.int16)))) / 255.0
        else:
            d = 0.0
        motion.append(d)
        if d >= cut_thr:
            cut_times.append(t)
        prev_gray = gray

        fa = 0.0
        if detector is not None:
            sh, sw = small.shape[:2]
            detector.setInputSize((sw, sh))
            try:
                _, faces = detector.detect(small)
            except Exception:  # noqa: BLE001
                faces = None
            if faces is not None and len(faces) > 0:
                best = max(float(f[2]) * float(f[3]) for f in faces)
                fa = min(1.0, best / float(sw * sh))
        face_area.append(fa)

        times.append(t)
        t += interval
    cap.release()

    if not times:
        return None
    log.info("visual analysis: %d samples, %d scene cuts", len(times), len(cut_times))
    return VisualSignals(times=times, motion=motion, face_area=face_area, cut_times=cut_times)
