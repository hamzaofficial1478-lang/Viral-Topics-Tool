"""Heuristic hook scorer (M3 fallback / default).

Zero external dependencies and no API key: scores each transcript segment on the
rubric signals and returns ranked candidates. This is the default backend when
no ANTHROPIC_API_KEY is present, and always available as a safety net.
"""

from __future__ import annotations

import re

from ..models import Candidate, Transcript
from . import rubric

_WORD_RE = re.compile(r"[a-z']+")


def _contains_any(text: str, needles: list[str]) -> list[str]:
    return [n for n in needles if n in text]


def score_segment_text(text: str) -> tuple[float, str, dict]:
    """Score a single segment's text. Returns (score0..1, reason, signals)."""
    low = text.lower()
    tokens = _WORD_RE.findall(low)
    n_words = len(tokens)
    signals: dict = {}
    raw = 0.0
    reasons: list[str] = []

    phrases = _contains_any(low, rubric.HOOK_PHRASES)
    if phrases:
        raw += 3.0
        signals["hook_phrases"] = phrases
        reasons.append(f'opens a loop ("{phrases[0]}")')

    if "?" in text:
        raw += 2.0
        signals["question"] = True
        reasons.append("asks a question")

    has_digit = bool(re.search(r"\d", text))
    number_words = [w for w in tokens if w in rubric.NUMBER_WORDS]
    if has_digit or number_words:
        raw += 2.0
        signals["number"] = True
        if "%" in text or "percent" in low:
            raw += 1.0
            signals["stat"] = True
        reasons.append("cites a number/stat")

    strong = [w for w in tokens if w in rubric.STRONG_WORDS]
    if strong:
        raw += min(2.0, 1.0 + 0.5 * len(set(strong)))
        signals["strong_words"] = sorted(set(strong))
        reasons.append(f'strong claim ("{strong[0]}")')

    emotion = [w for w in tokens if w in rubric.EMOTION_WORDS]
    if emotion:
        raw += 1.0
        signals["emotion_words"] = sorted(set(emotion))
        reasons.append("emotional peak")

    if "you" in tokens or "your" in tokens:
        raw += 1.0
        signals["second_person"] = True

    if "!" in text:
        raw += 1.0
        signals["exclamation"] = True

    # Length sweet spot: a self-contained sentence, not a fragment or a ramble.
    if 6 <= n_words <= 40:
        raw += 1.0
        signals["good_length"] = True
    elif n_words < 4:
        raw -= 1.0
    elif n_words > 60:
        raw -= 1.0

    score = max(0.0, min(1.0, raw / rubric.HEURISTIC_SCALE))
    reason = "; ".join(reasons) if reasons else "conversational segment"
    return score, reason, signals


def detect(transcript: Transcript, min_score: float = 0.0) -> list[Candidate]:
    """Score every segment and return candidates ranked by score (desc)."""
    candidates: list[Candidate] = []
    for seg in transcript.segments:
        score, reason, signals = score_segment_text(seg.text)
        if score >= min_score:
            candidates.append(
                Candidate(
                    start=seg.start,
                    end=seg.end,
                    score=score,
                    reason=reason,
                    signals=signals,
                )
            )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates
