"""M4 — Clip selection + the "how many clips?" recommender.

Turns ranked hook candidates into concrete clips. Each clip is a *continuous*
passage of the source (never random splices) grown from a high-scoring anchor.
In coherent mode (default), boundaries snap to natural thought/topic breaks —
pauses in the speech — so a clip begins at the start of an idea and ends on its
payoff, playing as a complete little story rather than an arbitrary window.
"""

from __future__ import annotations

from ..config import Config
from ..models import Candidate, Clip, Transcript

_STRONG = 0.5   # candidate score considered a "strong standalone moment"
_MAX_CLIPS = 20
_MIN_CLIP_SECONDS = 6.0

# Words that signal a segment continues a previous thought — a bad place to
# START a clip unless there was a real pause before it.
_CONTINUATIONS = {
    "and", "but", "so", "or", "nor", "yet", "because", "which", "that", "then",
    "also", "plus", "however", "therefore", "thus", "although", "though",
    "whereas", "while", "since", "besides", "anyway",
}


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

    coherent = bool(cfg.get("select.coherent", True))
    pause_thr = float(cfg.get("select.pause_threshold", 0.5))
    gaps = _gaps(segments)

    seg_score = _score_by_segment(transcript, candidates)
    # Anchor order: highest-scoring segments first.
    order = sorted(
        range(len(segments)),
        key=lambda i: seg_score[i].score if i in seg_score else 0.0,
        reverse=True,
    )

    # STEP 3.5: don't anchor clips on low-confidence (likely garbled) segments.
    min_conf = float(cfg.get("transcribe.min_confidence", 0.0) or 0.0)

    used: set[int] = set()
    chosen: list[tuple[int, int, Candidate]] = []  # (lo, hi, anchor candidate)

    for anchor in order:
        if len(chosen) >= n:
            break
        if anchor in used:
            continue
        if min_conf > 0:
            c = getattr(segments[anchor], "confidence", None)
            if c is not None and c < min_conf:
                continue
        if coherent:
            lo, hi = _grow_coherent(
                anchor, segments, gaps, used, lower, upper, target, pause_thr
            )
        else:
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


def _gaps(segments: list) -> list[float]:
    """Pause (seconds) after each segment; last entry is +inf (end of video)."""
    gaps = []
    for i in range(len(segments)):
        if i + 1 < len(segments):
            gaps.append(max(0.0, segments[i + 1].start - segments[i].end))
        else:
            gaps.append(float("inf"))
    return gaps


def _first_word(text: str) -> str:
    parts = text.strip().split()
    return parts[0].lower().strip(",.!?;:") if parts else ""


def _is_thought_start(i: int, segments: list, gaps: list[float], pause_thr: float) -> bool:
    """True if segment ``i`` cleanly begins a thought (not mid-sentence)."""
    if i == 0:
        return True
    if _first_word(segments[i].text) in _CONTINUATIONS:
        # A continuation word only starts a clip cleanly after a real pause.
        return gaps[i - 1] >= pause_thr
    return True


def _is_strong_end(i: int, segments: list, gaps: list[float], pause_thr: float) -> bool:
    """True if segment ``i`` is followed by a pause (a thought/topic boundary)."""
    return gaps[i] >= pause_thr


def _grow_coherent(
    anchor: int,
    segments: list,
    gaps: list[float],
    used: set[int],
    lower: float,
    upper: float,
    target: float,
    pause_thr: float,
) -> tuple[int | None, int | None]:
    """Grow a clip that starts on a thought-start and ends on its payoff.

    1. Back up (bounded) so we don't begin mid-thought.
    2. Grow forward to reach the minimum length.
    3. Extend to the nearest pause (natural end) within tolerance, else to the
       segment closest to the target duration.
    """
    if anchor in used:
        return None, None
    n = len(segments)
    lo = hi = anchor

    # 1) Back up to a clean thought start (at most a few segments, within upper).
    back = 0
    while (
        lo - 1 >= 0
        and (lo - 1) not in used
        and back < 3
        and not _is_thought_start(lo, segments, gaps, pause_thr)
        and segments[hi].end - segments[lo - 1].start <= upper
    ):
        lo -= 1
        back += 1

    # 2) Grow forward (preferred) to reach the lower bound.
    while segments[hi].end - segments[lo].start < lower:
        if hi + 1 < n and (hi + 1) not in used and \
                segments[hi + 1].end - segments[lo].start <= upper:
            hi += 1
        elif lo - 1 >= 0 and (lo - 1) not in used and \
                segments[hi].end - segments[lo - 1].start <= upper:
            lo -= 1
        else:
            break

    # 3) Extend to a natural end (pause) within upper; else closest to target.
    best_hi = hi
    while hi + 1 < n and (hi + 1) not in used:
        if segments[hi + 1].end - segments[lo].start > upper:
            break
        hi += 1
        if _is_strong_end(hi, segments, gaps, pause_thr):
            best_hi = hi
            break
        cur = abs((segments[hi].end - segments[lo].start) - target)
        prev = abs((segments[best_hi].end - segments[lo].start) - target)
        if cur < prev:
            best_hi = hi
    hi = best_hi

    return lo, hi


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
