"""M3 — Hook / highlight detection.

``detect_hooks`` dispatches to the Claude scorer when available, otherwise the
keyword heuristic. Both return candidates ranked by score.
"""

from __future__ import annotations

import os

from ..config import Config
from ..models import Candidate, Transcript
from ..utils import log
from . import heuristic


def _llm_available() -> bool:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def detect_hooks(transcript: Transcript, cfg: Config) -> list[Candidate]:
    backend = cfg.get("detect.backend", "auto")
    min_score = float(cfg.get("detect.min_segment_score", 0.0))

    want_llm = backend == "llm" or (backend == "auto" and _llm_available())
    if want_llm:
        try:
            from . import llm

            candidates = llm.detect(transcript, cfg)
            filtered = [c for c in candidates if c.score >= min_score]
            log.info(
                "hook detection: Claude (%d/%d segments above %.2f)",
                len(filtered),
                len(candidates),
                min_score,
            )
            return filtered
        except Exception as e:  # noqa: BLE001 - fall back on any LLM failure
            log.warning("LLM hook detection unavailable (%s); using heuristic", e)

    candidates = heuristic.detect(transcript, min_score)
    log.info(
        "hook detection: heuristic (%d segments above %.2f)",
        len(candidates),
        min_score,
    )
    return candidates


__all__ = ["detect_hooks"]
