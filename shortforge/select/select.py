"""M4 — Clip selection + the "how many clips?" recommender.

Turns ranked hook candidates into concrete clips by growing each high-scoring
anchor segment outward across whole neighbouring segments until it reaches the
target duration. Growing by whole segments keeps clip boundaries on clean
sentence starts/ends and keeps clips self-contained.
"""

from __future__ import annotations

from ..config import Config
from ..models import Candidate, Clip, Transcript

_STRONG = 0.5   # candidate score considered a "strong standalone moment"
_MAX_CLIPS = 20
_MIN_CLIP_SECONDS = 6.0


def recommend_clip_count(
    transcript: Transcript, candidates: list[Candidate], cfg: Config
) -> tuple[int, str]:
    """Recommend N clips from video length and count of strong moments."""
    target = float(cfg.get("select.target_duration", 45))
    strong = sum(1 for c in candidates if c.score >= _STRONG)
    by_length = int(transcript.duration // max(1.0, target))

    if strong > 0:
        n = max(1, min(strong, by_length if by_length else strong, _MAX_CLIPS))
    else:
        n = max(1, min(by_length or 1, 3))

    mins = transcript.duration / 60.0
    rationale = (
        f"This {mins:.0f}-min video has {strong} strong standalone moment"
        f"{'s' if strong != 1 else ''}; room for ~{by_length} clips of "
        f"{target:.0f}s. Recommending {n} clip{'s' if n != 1 else ''}."
    )
    return n, rationale


def _score_by_segment(transcript: Transcript, candidates: list[Candidate]) -> dict[int, Candidate]:
    """Map each segment index to its candidate (by matching start time)."""
    starts = {round(s.start, 3): i for i, s in enumerate(transcript.segments)}
    out: dict[int, Candidate] = {}
    for c in candidates:
        i = starts.get(round(c.start, 3))
        if i is not None:
            out[i] = c
    return out


def build_clips(
    transcript: Transcript,
    candidates: list[Candidate],
    cfg: Config,
    source_hash: str,
) -> list[Clip]:
    """Select up to N clips, snapped to sentence boundaries."""
    segments = transcript.segments
    if not segments:
        return []

    target = float(cfg.get("select.target_duration", 45))
    tol = float(cfg.get("select.tolerance", 12))
    width = int(cfg.get("reframe.width", 1080))
    height = int(cfg.get("reframe.height", 1920))
    resolution = f"{width}x{height}"

    requested = int(cfg.get("select.num_clips", 0) or 0)
    if requested > 0:
        n = min(requested, _MAX_CLIPS)
    else:
        n, _ = recommend_clip_count(transcript, candidates, cfg)

    # Never grow to a window shorter than the minimum clip length, even when
    # target-tol dips below it (small target / large tolerance).
    lower = max(target - tol, _MIN_CLIP_SECONDS)
    upper = max(target + tol, lower)

    seg_score = _score_by_segment(transcript, candidates)
    # Anchor order: highest-scoring segments first.
    order = sorted(
        range(len(segments)),
        key=lambda i: seg_score[i].score if i in seg_score else 0.0,
        reverse=True,
    )

    used: set[int] = set()
    chosen: list[tuple[int, int, Candidate]] = []  # (lo, hi, anchor candidate)

    for anchor in order:
        if len(chosen) >= n:
            break
        if anchor in used:
            continue
        lo, hi = _grow(anchor, segments, used, lower, upper)
        if lo is None:
            continue
        dur = segments[hi].end - segments[lo].start
        if dur < _MIN_CLIP_SECONDS:
            continue
        for i in range(lo, hi + 1):
            used.add(i)
        anchor_cand = seg_score.get(anchor, Candidate(
            start=segments[anchor].start, end=segments[anchor].end, score=0.0))
        chosen.append((lo, hi, anchor_cand))

    # Emit in chronological order; keep the rank/score for the manifest.
    chosen.sort(key=lambda t: segments[t[0]].start)
    clips: list[Clip] = []
    for idx, (lo, hi, anchor) in enumerate(chosen):
        start = segments[lo].start
        end = min(segments[hi].end, transcript.duration or segments[hi].end)
        text = " ".join(s.text.strip() for s in segments[lo : hi + 1]).strip()
        clips.append(
            Clip(
                clip_id=f"{idx + 1:02d}",
                source_hash=source_hash,
                start=start,
                end=end,
                score=round(anchor.score, 3),
                reason=anchor.reason,
                caption_text=text,
                resolution=resolution,
            )
        )
    return clips


def _grow(
    anchor: int,
    segments: list,
    used: set[int],
    lower: float,
    upper: float,
) -> tuple[int | None, int | None]:
    """Grow [lo, hi] outward from ``anchor`` to within [lower, upper] seconds.

    Adds whole neighbouring segments, preferring forward continuation, without
    crossing into already-used segments. Returns (None, None) if the anchor is
    unusable.
    """
    if anchor in used:
        return None, None
    lo = hi = anchor

    while True:
        dur = segments[hi].end - segments[lo].start
        if dur >= lower:
            break
        can_fwd = hi + 1 < len(segments) and (hi + 1) not in used
        can_bwd = lo - 1 >= 0 and (lo - 1) not in used

        # Don't overshoot past the tolerance window once we have enough.
        if can_fwd:
            nxt = segments[hi + 1].end - segments[lo].start
            if nxt <= upper or dur < lower:
                hi += 1
                continue
        if can_bwd:
            prv = segments[hi].end - segments[lo - 1].start
            if prv <= upper or dur < lower:
                lo -= 1
                continue
        break

    return lo, hi
