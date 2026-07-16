"""LLM hook scorer (M3 primary, optional).

Uses Claude to score transcript segments against the shared rubric. Requires the
``anthropic`` package and credentials (ANTHROPIC_API_KEY). If either is missing
the dispatcher in ``__init__`` falls back to the keyword heuristic — this module
never has to be present for the pipeline to run.
"""

from __future__ import annotations

import json

from ..config import Config
from ..models import Candidate, Transcript
from ..utils import log
from . import rubric

_BATCH = 100  # segments per API call

_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "score": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "score", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}


def detect(transcript: Transcript, cfg: Config) -> list[Candidate]:
    """Score every segment with Claude. Raises on failure (caller falls back)."""
    import anthropic  # imported lazily so the dep stays optional

    client = anthropic.Anthropic()
    model = cfg.get("detect.llm_model", "claude-opus-4-8")

    segments = transcript.segments
    scores: dict[int, tuple[float, str]] = {}

    for base in range(0, len(segments), _BATCH):
        chunk = segments[base : base + _BATCH]
        listing = "\n".join(
            f"{base + i}\t[{s.start:.1f}-{s.end:.1f}] {s.text}"
            for i, s in enumerate(chunk)
        )
        prompt = (
            f"{rubric.LLM_RUBRIC}\n\n"
            "Score each line below from 0.0 (weak) to 1.0 (strong hook). "
            "Return one entry per line index with a one-line reason.\n\n"
            "index<TAB>[start-end] text:\n"
            f"{listing}"
        )
        # Structured output guarantees valid JSON matching _SCHEMA; effort low
        # keeps this bulk classification pass cheap.
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        data = json.loads(text)
        for item in data.get("scores", []):
            idx = int(item["index"])
            score = max(0.0, min(1.0, float(item["score"])))
            scores[idx] = (score, str(item.get("reason", "")).strip())

    candidates: list[Candidate] = []
    for i, seg in enumerate(segments):
        score, reason = scores.get(i, (0.0, ""))
        candidates.append(
            Candidate(
                start=seg.start,
                end=seg.end,
                score=score,
                reason=reason or "scored by Claude",
                signals={"backend": "llm"},
            )
        )
    candidates.sort(key=lambda c: c.score, reverse=True)
    log.info("LLM scored %d segments", len(candidates))
    return candidates
