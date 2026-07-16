"""M10 — Metadata generation (Phase 2).

Per-clip title / description / hashtags / first-comment. Heuristic by default
(no key needed); optional Claude when ANTHROPIC_API_KEY + anthropic are present.
Niche/tone come from an optional per-page profile (Section 8) in config.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..config import Config
from ..models import Clip
from ..utils import log

_STOP = {
    "the", "and", "for", "that", "this", "with", "you", "your", "are", "was",
    "but", "not", "have", "has", "will", "can", "all", "get", "got", "one",
    "they", "them", "our", "out", "who", "how", "why", "what", "when", "who",
    "from", "into", "about", "just", "like", "than", "then", "there", "here",
    "some", "most", "more", "very", "much", "many", "make", "made", "does",
    "did", "because", "were", "been", "being", "would", "could", "should",
}
_WORD = re.compile(r"[A-Za-z']+")


def _first_sentence(text: str, limit: int = 80) -> str:
    text = text.strip()
    m = re.search(r"[.!?]", text)
    s = text[: m.start() + 1] if m else text
    s = s.strip().rstrip(".")
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "..."
    return s


def _keywords(text: str, n: int) -> list[str]:
    counts: dict[str, int] = {}
    for w in _WORD.findall(text.lower()):
        if len(w) > 3 and w not in _STOP:
            counts[w] = counts.get(w, 0) + 1
    ranked = sorted(counts, key=lambda k: (-counts[k], k))
    return ranked[:n]


def _heuristic(clip: Clip, cfg: Config) -> dict[str, Any]:
    text = clip.caption_text or ""
    niche = cfg.get("page.niche") or ""
    tone = cfg.get("page.tone") or ""
    base_tags = cfg.get("page.hashtags") or []

    title = _first_sentence(text) or "Watch this"
    if title and title[0].islower():
        title = title[0].upper() + title[1:]

    kw = _keywords(text, 6)
    tags = ["#shorts", "#reels"]
    tags += [f"#{re.sub(r'[^a-z0-9]', '', n.lower())}" for n in niche.split() if n]
    tags += [f"#{re.sub(r'[^a-z0-9]', '', t.strip().lstrip('#').lower())}"
             for t in base_tags if t]
    tags += [f"#{w}" for w in kw]
    # dedupe, keep order, cap 8
    seen, out_tags = set(), []
    for t in tags:
        if len(t) > 1 and t not in seen:
            seen.add(t)
            out_tags.append(t)
        if len(out_tags) >= 8:
            break

    desc = _first_sentence(text, 150)
    if tone:
        desc = f"{desc}"  # tone reserved for the LLM path
    description = (desc + "\n\n" + " ".join(out_tags)).strip()
    first_comment = "What do you think? Drop a comment 👇"

    return {
        "title": title,
        "description": description,
        "hashtags": out_tags,
        "first_comment": first_comment,
        "backend": "heuristic",
    }


def _llm_available() -> bool:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _llm(clip: Clip, cfg: Config) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic()
    model = cfg.get("detect.llm_model", "claude-opus-4-8")
    niche = cfg.get("page.niche") or "general"
    tone = cfg.get("page.tone") or "punchy, direct"
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "first_comment": {"type": "string"},
        },
        "required": ["title", "description", "hashtags", "first_comment"],
        "additionalProperties": False,
    }
    prompt = (
        f"Write short-form metadata for this clip. Niche: {niche}. Tone: {tone}.\n"
        f"Hook-first title (<=70 chars), a 1-2 sentence description, 3-8 hashtags, "
        f"and a first-comment CTA. Advertiser-friendly.\n\nTranscript:\n{clip.caption_text}"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=1000,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    import json

    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = json.loads(text)
    data["backend"] = "llm"
    return data


def generate(clip: Clip, cfg: Config) -> dict[str, Any]:
    backend = cfg.get("metadata.backend", "auto")
    want_llm = backend == "llm" or (backend == "auto" and _llm_available())
    if want_llm:
        try:
            return _llm(clip, cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM metadata unavailable (%s); using heuristic", e)
    return _heuristic(clip, cfg)
