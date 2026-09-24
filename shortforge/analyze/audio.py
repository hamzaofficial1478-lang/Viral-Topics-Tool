"""M9 — auto-edit audio helpers.

- Loudness normalisation to ~-14 LUFS (the social standard). Applied as an
  ffmpeg ``loudnorm`` audio filter in the render step.
- Silence / dead-air planning: from the word-level timestamps we already have,
  compute which source-time ranges to KEEP (speech + a little padding), dropping
  long gaps. The render step can concatenate the kept ranges (jump cuts) and the
  captions are remapped onto the compressed timeline so they stay in sync.
"""

from __future__ import annotations

from ..config import Config
from ..models import Word


def loudnorm_filter(cfg: Config) -> str | None:
    """ffmpeg loudnorm audio-filter string, or None if disabled."""
    if not cfg.get("render.loudnorm", True):
        return None
    target = float(cfg.get("render.loudnorm_i", -14.0))
    tp = float(cfg.get("render.loudnorm_tp", -1.5))
    lra = float(cfg.get("render.loudnorm_lra", 11.0))
    return f"loudnorm=I={target}:TP={tp}:LRA={lra}"


def plan_keep_ranges(
    words: list[Word],
    clip_start: float,
    clip_end: float,
    *,
    min_silence: float = 0.8,
    max_gap: float = 0.35,
    pad: float = 0.1,
) -> list[tuple[float, float]]:
    """Compute source-time ranges to keep, trimming dead air.

    Gaps between words longer than ``min_silence`` are collapsed to ``max_gap``.
    Each speech run is padded by ``pad`` seconds (clamped to the clip bounds).
    Returns merged, non-overlapping ranges in source time within
    [clip_start, clip_end]. With no words, keeps the whole clip.
    """
    inside = [w for w in words if w.end > clip_start and w.start < clip_end]
    if not inside:
        return [(clip_start, clip_end)]

    ranges: list[tuple[float, float]] = []
    for w in inside:
        s = max(clip_start, w.start - pad)
        e = min(clip_end, w.end + pad)
        if not ranges:
            ranges.append((s, e))
            continue
        ps, pe = ranges[-1]
        gap = s - pe
        if gap <= min_silence:
            # Keep them joined (collapse the small/medium gap entirely).
            ranges[-1] = (ps, max(pe, e))
        else:
            # Long dead air: keep only max_gap of it as breathing room.
            ranges[-1] = (ps, pe + max_gap)
            ranges.append((s, e))

    # Clamp and drop empties.
    out = []
    for s, e in ranges:
        s = max(clip_start, s)
        e = min(clip_end, e)
        if e > s + 1e-3:
            out.append((s, e))
    return out or [(clip_start, clip_end)]


def total_kept(ranges: list[tuple[float, float]]) -> float:
    return sum(e - s for s, e in ranges)


def remap_time(t_src: float, ranges: list[tuple[float, float]]) -> float:
    """Map a source-time instant onto the compressed (kept) timeline."""
    acc = 0.0
    for s, e in ranges:
        if t_src < s:
            return acc
        if t_src <= e:
            return acc + (t_src - s)
        acc += e - s
    return acc


def remap_words(words: list[Word], ranges: list[tuple[float, float]]) -> list[Word]:
    """Remap words onto the compressed timeline (drop words fully in dead air)."""
    out: list[Word] = []
    for w in words:
        # Keep a word if its span overlaps any kept range.
        if any(w.end > s and w.start < e for s, e in ranges):
            out.append(
                Word(
                    start=remap_time(w.start, ranges),
                    end=remap_time(w.end, ranges),
                    text=w.text,
                )
            )
    return out


def select_expr(ranges: list[tuple[float, float]], clip_start: float) -> str:
    """ffmpeg select/aselect expression keeping only the kept ranges.

    Timestamps are clip-relative (the render seeks to ``clip_start`` first).
    """
    parts = [
        f"between(t,{max(0.0, s - clip_start):.3f},{max(0.0, e - clip_start):.3f})"
        for s, e in ranges
    ]
    return "+".join(parts) if parts else "1"
