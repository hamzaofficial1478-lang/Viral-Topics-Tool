"""A3 — burned-in source-caption detection + treatment.

Some sources have captions baked into the pixels (not a soft subtitle track we
can strip). If we composite our target-language captions on top, both are
visible. This module (1) detects a persistent high-text-density horizontal band
(usually the lower third) across sampled frames, and (2) emits an ffmpeg
sub-graph that covers / blurs / crops that band before our captions go on.

Detection uses OpenCV edge density; if OpenCV is unavailable it reports nothing
(the pipeline simply proceeds, logging that detection was skipped).
"""

from __future__ import annotations

from ..config import Config
from ..utils import log


def available() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


def detect(source_path: str, src_w: int, src_h: int, cfg: Config) -> dict | None:
    """Return {"y": px, "h": px, "confidence": f, "where": "bottom|top"} or None."""
    if not available():
        log.info("burned-in caption detection skipped (OpenCV unavailable)")
        return None
    import cv2
    import numpy as np

    n = int(cfg.get("captions.burned_in_samples", 16))
    floor = float(cfg.get("captions.burned_in_threshold", 0.025))
    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        return None
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    if total <= 0:
        cap.release()
        return None

    # Accumulate edge density per horizontal strip (rows) across sampled frames.
    strips = 24
    acc = np.zeros(strips, dtype=np.float64)
    got = 0
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / n))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        got += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 100, 200)
        h = edges.shape[0]
        for s in range(strips):
            y0, y1 = int(h * s / strips), int(h * (s + 1) / strips)
            acc[s] += float(np.count_nonzero(edges[y0:y1])) / max(1, (y1 - y0) * edges.shape[1])
    cap.release()
    if got == 0:
        return None
    acc /= got

    # Relative threshold: a caption band stands out ABOVE the frame's baseline
    # edge density (so it works on both plain and busy footage), with an
    # absolute floor to avoid flagging noise.
    baseline = float(np.median(acc))
    thr = max(floor, baseline + 0.015)

    # A caption band = contiguous strips above threshold, biased to the
    # lower/upper thirds (where captions live), not the busy middle.
    band = _best_band(acc, thr)
    if band is None:
        log.info("no burned-in caption band detected")
        return None
    s0, s1, conf = band
    y = int(src_h * s0 / strips)
    h = int(src_h * (s1 - s0) / strips)
    where = "bottom" if s0 >= strips // 2 else "top"
    log.info("burned-in text detected: %s band y=%d..%d (conf %.2f)",
             where, y, y + h, conf)
    return {"y": y, "h": h, "confidence": round(conf, 3), "where": where}


def _best_band(acc, thr: float):
    """Find the strongest contiguous above-threshold band in the top/bottom third."""
    strips = len(acc)
    third = max(1, strips // 3)
    best = None
    s = 0
    while s < strips:
        if acc[s] >= thr:
            e = s
            while e + 1 < strips and acc[e + 1] >= thr:
                e += 1
            # only accept bands sitting in the lower or upper third
            in_lower = s >= strips - third
            in_upper = e < third
            if in_lower or in_upper:
                conf = float(sum(acc[s:e + 1]) / (e - s + 1))
                if best is None or conf > best[2]:
                    best = (s, e + 1, conf)
            s = e + 1
        else:
            s += 1
    return best


def subgraph(in_label: str, out_label: str, band: dict, mode: str) -> str:
    """ffmpeg filter_complex fragment treating the band on ``in_label``.

    mode: cover (opaque box) | blur (localized blur) | crop (remove the band).
    Returns a fragment ending in ``[out_label]`` (geometry preserved for
    cover/blur; crop trims the band off the matching edge).
    """
    y, h = int(band["y"]), int(band["h"])
    if mode == "cover":
        return f"[{in_label}]drawbox=x=0:y={y}:w=iw:h={h}:color=black:t=fill[{out_label}]"
    if mode == "blur":
        return (
            f"[{in_label}]split=2[bi0][bi1];"
            f"[bi1]crop=iw:{h}:0:{y},boxblur=20:2[bib];"
            f"[bi0][bib]overlay=0:{y}[{out_label}]"
        )
    if mode == "crop":
        # Remove the band from the matching edge; reframe re-scales afterwards.
        if band.get("where") == "top":
            return f"[{in_label}]crop=iw:ih-{h}:0:{h}[{out_label}]"
        return f"[{in_label}]crop=iw:ih-{h}:0:0[{out_label}]"
    return f"[{in_label}]null[{out_label}]"


def treat_frame(frame, band: dict, mode: str):
    """Apply cover/blur to a single OpenCV frame (tracked-render path)."""
    import cv2

    y, h = int(band["y"]), int(band["h"])
    y = max(0, min(y, frame.shape[0] - 1))
    h = max(1, min(h, frame.shape[0] - y))
    if mode == "blur":
        region = frame[y:y + h, :]
        frame[y:y + h, :] = cv2.GaussianBlur(region, (0, 0), 12)
    else:  # cover (and crop-fallback in the tracked path)
        cv2.rectangle(frame, (0, y), (frame.shape[1], y + h), (0, 0, 0), -1)
    return frame
