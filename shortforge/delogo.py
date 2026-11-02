"""Find and remove logos, watermarks and wordmarks burned into a video.

Detection: a logo stays put while the picture behind it changes. The per-pixel
median of every sampled frame's edges cancels moving content out and leaves only
what is printed on top — down to about 3% opacity. Removal fills each found area
from the pixels around it (FFmpeg ``delogo``; OpenCV inpaint on the
tracked-render path).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field

from .config import Config
from .utils import ShortForgeError, content_key, ffprobe_info, log, require_binary, run

_VERSION = 1                     # bump when detection changes, so caches refresh
_ANALYSIS_SIDE = 720             # frames are analysed at this longer side
_DYNAMIC_STD = 6.0               # gray levels: below this a pixel is "still"


@dataclass
class Detection:
    width: int
    height: int
    regions: list[dict] = field(default_factory=list)
    mask_path: str | None = None
    frames: int = 0
    note: str = ""
    _mask: object = field(default=None, repr=False, compare=False)

    @property
    def found(self) -> bool:
        return bool(self.regions and self.mask_path)

    def describe(self) -> str:
        if not self.found:
            return "logo removal: none found"
        n = len(self.regions)
        return f"logo removed ({n} area{'s' if n != 1 else ''})"

    def to_dict(self) -> dict:
        return {"width": self.width, "height": self.height, "regions": self.regions,
                "mask_path": self.mask_path, "frames": self.frames, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "Detection":
        return cls(width=int(d["width"]), height=int(d["height"]),
                   regions=list(d.get("regions") or []), mask_path=d.get("mask_path"),
                   frames=int(d.get("frames") or 0), note=d.get("note") or "")


def enabled(cfg: Config) -> bool:
    return bool(cfg.get("delogo.enabled", False))


def _grab_gray(ffmpeg: str, path: str, t: float):
    import cv2
    import numpy as np
    cmd = [ffmpeg, "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path,
           "-frames:v", "1", "-an", "-sn", "-vf", "format=gray",
           "-c:v", "pgm", "-f", "image2pipe", "pipe:1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return cv2.imdecode(np.frombuffer(proc.stdout, np.uint8), cv2.IMREAD_GRAYSCALE)


def _sample_frames(path: str, n: int):
    """Grayscale frames spread across the whole video: (small frames, full W, full H)."""
    import cv2
    ffmpeg = require_binary("ffmpeg")
    duration = ffprobe_info(path).duration or 0.0
    if duration <= 0:
        raise ShortForgeError(f"Logo removal: could not read the length of {path}.")
    lo, hi = duration * 0.02, duration * 0.98
    times = [lo + (hi - lo) * (i + 0.5) / n for i in range(n)]
    small, full_w, full_h, scale = [], 0, 0, 1.0
    for t in times:
        g = _grab_gray(ffmpeg, path, t)
        if g is None:
            continue
        if not full_w:
            full_h, full_w = g.shape[:2]
            scale = min(1.0, _ANALYSIS_SIDE / float(max(full_w, full_h)))
        if g.shape[:2] != (full_h, full_w):
            continue
        if scale < 1.0:
            g = cv2.resize(g, (max(8, round(full_w * scale)), max(8, round(full_h * scale))),
                           interpolation=cv2.INTER_AREA)
        small.append(g)
    return small, full_w, full_h


def analyse(frames, *, max_area: float = 0.15) -> tuple:
    """Find fixed overlays in same-size grayscale frames.

    Returns (mask uint8 0/255 at frame size, regions [{x,y,w,h,kind,strength}], notes).
    """
    import cv2
    import numpy as np

    stack = np.stack(frames).astype(np.float32)
    n, h, w = stack.shape
    gx = np.empty_like(stack)
    gy = np.empty_like(stack)
    for i in range(n):
        # Only sharp detail counts: lighting, vignettes and soft backgrounds are removed.
        hp = stack[i] - cv2.GaussianBlur(stack[i], (0, 0), 2.5)
        gx[i] = cv2.Sobel(hp, cv2.CV_32F, 1, 0, ksize=3)
        gy[i] = cv2.Sobel(hp, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(np.median(gx, axis=0), np.median(gy, axis=0))
    del gx, gy
    dynamic = stack.std(axis=0) > _DYNAMIC_STD
    del stack

    # Smoothing keeps a faint logo's strokes (lines) and flattens lone noise specks.
    mag = cv2.GaussianBlur(mag, (0, 0), 1.2)
    b = 4
    # The median is noise in every case, however big or strong the logo is.
    thr = max(1.5, 3.5 * float(np.median(mag[b:-b, b:-b])))
    cand = (mag > thr).astype(np.uint8) * 255
    cand[:b, :] = 0
    cand[-b:, :] = 0
    cand[:, :b] = 0
    cand[:, -b:] = 0

    k = max(3, round(max(w, h) * 0.012)) | 1
    joined = cv2.morphologyEx(cand, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)

    frame_area = float(w * h)
    min_px = max(20, int(frame_area * 0.0002))
    ring_in = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    ring_out = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3 * k, 3 * k))
    margin_x, margin_y = b + max(2, int(w * 0.01)), b + max(2, int(h * 0.01))
    picked, notes = [], []
    for i in range(1, count):
        x, y, bw, bh, _area = (int(v) for v in stats[i])
        comp = (labels == i).astype(np.uint8) * 255
        if int(np.count_nonzero(cand[comp > 0])) < min_px:
            continue
        if bw > 0.6 * w or bh > 0.6 * h:
            notes.append("ignored a long line/border (frame edge or letterbox bar)")
            continue
        ring = cv2.dilate(comp, ring_out) & ~cv2.dilate(comp, ring_in)
        ring_px = ring > 0
        moving_behind = float(dynamic[ring_px].mean()) if ring_px.any() else 0.0
        strength = float(mag[comp > 0].mean())
        box_share = (bw * bh) / frame_area
        near_x = x < 0.28 * w or x + bw > 0.72 * w
        near_y = y < 0.22 * h or y + bh > 0.78 * h
        clear_of_edge = (x >= margin_x and y >= margin_y
                         and x + bw <= w - margin_x and y + bh <= h - margin_y)
        if moving_behind >= 0.5:
            kind = "overlay"
        elif (near_x or near_y) and box_share <= 0.025 and clear_of_edge:
            kind = "corner"
        else:
            notes.append("ignored a fixed area in a still part of the picture "
                         "(looks like scenery, not a logo)")
            continue
        picked.append({"label": i, "x": x, "y": y, "w": bw, "h": bh, "kind": kind,
                       "strength": round(strength, 1), "share": box_share})

    picked.sort(key=lambda r: (r["kind"] != "overlay", -r["strength"]))
    kept, total = [], 0.0
    for r in picked:
        if total + r["share"] > max_area:
            notes.append(f"skipped a {r['kind']} area: treating it would cover more than "
                         f"{max_area:.0%} of the picture")
            continue
        kept.append(r)
        total += r["share"]

    parts = np.zeros((h, w), np.uint8)
    kind_of = {}
    for r in kept:
        parts[labels == r["label"]] = 255
        kind_of[r["label"]] = (r["kind"], r["strength"])

    # Pieces of one logo (letters, a faint ring) become one solid shape.
    groups_n, groups = cv2.connectedComponents(
        cv2.dilate(parts, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3 * k, 3 * k))))
    mask = np.zeros((h, w), np.uint8)
    regions = []
    for g in range(1, groups_n):
        member = (groups == g) & (parts > 0)
        pts = cv2.findNonZero(member.astype(np.uint8))
        if pts is None:
            continue
        cv2.fillConvexPoly(mask, cv2.convexHull(pts), 255)
        x, y, bw, bh = cv2.boundingRect(pts)
        labs = set(np.unique(labels[member]).tolist()) & set(kind_of)
        kinds = [kind_of[lb] for lb in labs]
        regions.append({"x": x, "y": y, "w": bw, "h": bh,
                        "kind": "overlay" if any(kd == "overlay" for kd, _ in kinds) else "corner",
                        "strength": max(s for _, s in kinds)})
    if regions:
        grow = max(2, round(max(w, h) * 0.006)) | 1
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow)))
    return mask, regions, sorted(set(notes))


def _cache_key(path: str, samples: int, max_area: float) -> str:
    h = hashlib.blake2b(digest_size=8)
    h.update(f"{_VERSION}|{samples}|{max_area}".encode())
    return f"{content_key(path)}_{h.hexdigest()}"


def detect(path: str, cfg: Config, work_dir: str | None = None) -> Detection:
    """Look for fixed logos/watermarks in ``path``. Cached per file + settings.

    Raises ShortForgeError when the video cannot be analysed — removal was asked
    for, so carrying on with the logo still there would be a silent failure.
    """
    import cv2

    samples = max(12, int(cfg.get("delogo.samples", 48) or 48))
    max_area = float(cfg.get("delogo.max_area", 0.15) or 0.15)
    cache_dir = os.path.join(work_dir or cfg.get("paths.work_dir", ".shortforge"), "delogo")
    os.makedirs(cache_dir, exist_ok=True)
    key = _cache_key(path, samples, max_area)
    meta_path = os.path.join(cache_dir, f"{key}.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                det = Detection.from_dict(json.load(f))
            if not det.regions or (det.mask_path and os.path.isfile(det.mask_path)):
                log.info("logo removal: using the earlier scan of this video (%s)",
                         det.describe())
                return det
        except (OSError, ValueError, KeyError):
            pass

    log.info("logo removal: scanning %d frames across the video for logos/watermarks…",
             samples)
    frames, full_w, full_h = _sample_frames(path, samples)
    if len(frames) < 8:
        raise ShortForgeError(
            f"Logo removal was switched on, but only {len(frames)} frame(s) of this video "
            f"could be read, so it can't tell a logo from the picture. Turn logo removal "
            f"off for this video, or check the file plays.")
    mask, regions, notes = analyse(frames, max_area=max_area)
    for note in notes:
        log.info("logo removal: %s", note)

    det = Detection(width=full_w, height=full_h, frames=len(frames))
    if regions:
        sx, sy = full_w / float(mask.shape[1]), full_h / float(mask.shape[0])
        big = cv2.resize(mask, (full_w, full_h), interpolation=cv2.INTER_NEAREST)
        pad = max(1, round(max(sx, sy))) + round(0.004 * max(full_w, full_h))
        big = cv2.dilate(big, cv2.getStructuringElement(cv2.MORPH_RECT, (2 * pad + 1,) * 2))
        det.mask_path = os.path.abspath(os.path.join(cache_dir, f"{key}_mask.png"))
        if not cv2.imwrite(det.mask_path, big):
            raise ShortForgeError(f"Logo removal: could not write {det.mask_path}.")
        for r in regions:
            x0, y0 = max(0, int(r["x"] * sx) - pad), max(0, int(r["y"] * sy) - pad)
            x1 = min(full_w, int((r["x"] + r["w"]) * sx) + pad)
            y1 = min(full_h, int((r["y"] + r["h"]) * sy) + pad)
            det.regions.append({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                                "kind": r["kind"], "strength": r["strength"]})
        corner = sum(1 for r in regions if r["kind"] == "corner")
        det.note = (f"{len(regions)} area(s)"
                    + (f", {corner} on a still background (judged by position)" if corner else ""))
        for r in det.regions:
            log.info("logo removal: found %s at x=%d y=%d %dx%d", r["kind"], r["x"], r["y"],
                     r["w"], r["h"])
    else:
        det.note = "no fixed logo or watermark found"
        log.info("logo removal: no fixed logo or watermark found in this video")

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(det.to_dict(), f, indent=2)
    return det


def ffmpeg_filter(det: Detection) -> str | None:
    """A single-input, single-output filter chain that removes what was found."""
    if not det.found:
        return None
    parts = []
    for r in det.regions:
        x = min(max(1, r["x"]), det.width - 3)
        y = min(max(1, r["y"]), det.height - 3)
        bw = max(2, min(r["w"], det.width - 1 - x))
        bh = max(2, min(r["h"], det.height - 1 - y))
        parts.append(f"delogo=x={x}:y={y}:w={bw}:h={bh}")
    return ",".join(parts)


def treat_frame(frame, det: Detection):
    """Remove the found logo from one OpenCV BGR frame (tracked-render path)."""
    import cv2
    if not det.found:
        return frame
    if det._mask is None:
        m = cv2.imread(det.mask_path, cv2.IMREAD_GRAYSCALE)
        if m is None:
            raise ShortForgeError(f"Logo removal: mask missing: {det.mask_path}")
        ys, xs = (m > 0).nonzero()
        det._mask = (m, int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    m, x0, y0, x1, y1 = det._mask
    if m.shape[:2] != frame.shape[:2]:
        m = cv2.resize(m, (frame.shape[1], frame.shape[0]), interpolation=cv2.INTER_NEAREST)
        ys, xs = (m > 0).nonzero()
        det._mask = (m, int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        m, x0, y0, x1, y1 = det._mask
    pad = 8
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(frame.shape[1], x1 + pad), min(frame.shape[0], y1 + pad)
    roi = frame[y0:y1, x0:x1]
    frame[y0:y1, x0:x1] = cv2.inpaint(roi, m[y0:y1, x0:x1], 5, cv2.INPAINT_TELEA)
    return frame


def clean_file(src: str, dst: str, det: Detection) -> None:
    """Re-encode ``src`` to ``dst`` with the logo removed; audio is copied untouched."""
    vf = ffmpeg_filter(det)
    if not vf:
        raise ShortForgeError("clean_file called with nothing to remove (bug)")
    run([require_binary("ffmpeg"), "-y", "-v", "error", "-i", src,
         "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-vf", vf,
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", dst])
