"""M10 — Metadata generation (SEO title / description / tags).

Every clip gets discovery metadata that is **derived from what the clip actually
says**, not a genre template — so it matches the video, differs per clip, and is
built to be searched, not just displayed:

- **Title**: a hook the viewer wants to click, with the clip's primary keyphrase
  front-loaded for search.
- **Description**: a few informative, keyword-rich sentences that tell a viewer
  (and the algorithm) what the clip covers, plus a call-to-action and hashtags.
- **Tags**: analysis-based keywords/keyphrases pulled from the transcript — plain
  tags for the YouTube tag field and hashtag forms for captions/Shorts/Reels.

Heuristic by default (no key needed); Claude when ANTHROPIC_API_KEY + anthropic
are present produces sharper, more natural copy. Niche/tone come from an optional
per-page profile (Section 8) in config.
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
    "but", "not", "have", "has", "had", "will", "can", "all", "get", "got",
    "one", "they", "them", "their", "our", "out", "who", "how", "why", "what",
    "when", "where", "from", "into", "about", "just", "like", "than", "then",
    "there", "here", "some", "most", "more", "very", "much", "many", "make",
    "made", "does", "did", "because", "were", "been", "being", "would", "could",
    "should", "its", "it's", "i'm", "don't", "we", "us", "me", "my", "his",
    "her", "she", "him", "he", "so", "yeah", "okay", "gonna", "want", "really",
    "actually", "know", "think", "going", "say", "said", "thing", "things",
    "let", "lets", "let's", "now", "way", "too", "also", "even", "still",
}
_WORD = re.compile(r"[A-Za-z0-9']+")
# Words that make a sentence feel like a hook worth clicking.
_HOOK = {
    "how", "why", "what", "secret", "secrets", "mistake", "mistakes", "never",
    "always", "stop", "best", "worst", "biggest", "truth", "nobody", "everyone",
    "proven", "hack", "hacks", "trick", "tricks", "avoid", "reason", "should",
    "need", "warning", "before", "after", "instantly", "fast", "easy",
}


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _first_sentence(text: str, limit: int = 80) -> str:
    text = (text or "").strip()
    m = re.search(r"[.!?]", text)
    s = text[: m.start() + 1] if m else text
    s = s.strip().rstrip(".")
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "..."
    return s


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def _keyphrases(text: str, n: int = 12) -> list[str]:
    """Rank clean content keyphrases (bigrams + unigrams) from the transcript.

    Bigrams score higher (more specific / searchable). Phrases come only from
    runs of non-stopword tokens, so they read naturally and reflect what the
    clip is actually about — this is what makes the tags/title differ per video
    instead of being a fixed genre list. Selection avoids overlap so we don't
    emit "compound interest", "interest small", "small amounts" all at once.
    """
    tokens = _tokens(text)
    freq: dict[str, int] = {}
    for w in tokens:
        if len(w) > 2 and w not in _STOP and not w.isdigit():
            freq[w] = freq.get(w, 0) + 1

    bigrams: dict[str, float] = {}
    run: list[str] = []

    def flush(run: list[str]) -> None:
        for i in range(len(run) - 1):
            gram = f"{run[i]} {run[i + 1]}"
            bigrams[gram] = bigrams.get(gram, 0.0) + \
                (freq.get(run[i], 0) + freq.get(run[i + 1], 0)) * 1.5

    for w in tokens:
        if len(w) > 2 and w not in _STOP and not w.isdigit():
            run.append(w)
        else:
            flush(run)
            run = []
    flush(run)

    # Bigrams first (ranked), then unigrams — bigrams are more searchable.
    ranked_bi = sorted(bigrams, key=lambda k: (-bigrams[k], k))
    ranked_uni = sorted(freq, key=lambda k: (-freq[k], k))

    chosen: list[str] = []
    used: set[str] = set()  # tokens already represented by a chosen phrase

    # Non-overlapping bigrams: skip one sharing a token with an earlier pick.
    for p in ranked_bi:
        a, b = p.split(" ")
        if a in used or b in used:
            continue
        chosen.append(p)
        used.update((a, b))
        if len(chosen) >= n:
            return chosen

    # Fill remaining slots with the strongest standalone unigrams.
    for w in ranked_uni:
        if w in used:
            continue
        chosen.append(w)
        used.add(w)
        if len(chosen) >= n:
            break
    return chosen


def _titlecase_phrase(p: str) -> str:
    return " ".join(w if w.isupper() else w.capitalize() for w in p.split())


def _hashtagify(phrase: str) -> str:
    return "#" + re.sub(r"[^a-z0-9]", "", phrase.lower())


# Words a title should not end on after trimming (dangling connectors).
_DANGLE = {
    "to", "the", "a", "an", "of", "and", "or", "but", "because", "is", "are",
    "that", "this", "for", "with", "in", "on", "at", "your", "you", "we", "they",
    "it", "so", "if", "when", "how", "why", "as", "by", "from", "into", "our",
}


def _trim_title(s: str, limit: int) -> str:
    s = s.strip().rstrip(".").strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0]
    words = s.split()
    while words and words[-1].lower().strip(",;:!?") in _DANGLE:
        words.pop()
    s = " ".join(words).rstrip(",;:")
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s


def _hooked_title(text: str, phrases: list[str], limit: int = 70) -> str:
    """Build a click-worthy title, preferring a sentence that already carries a
    top keyphrase (front-loaded for search); inject one only if none does."""
    sentences = _sentences(text)
    top: set[str] = set()
    for p in phrases[:5]:
        top.update(p.split())

    def hook_score(s: str) -> tuple[int, int]:
        toks = _tokens(s)
        tset = set(toks)
        hooks = len(tset & _HOOK)
        kw = len(tset & top)
        has_q = 1 if s.rstrip().endswith("?") else 0
        has_num = 1 if any(t.isdigit() for t in toks) else 0
        fits = 1 if len(s) <= limit else 0
        return (hooks * 2 + kw + has_q * 2 + has_num + fits, -abs(len(s) - 55))

    if not sentences:
        return _trim_title(text or "Watch this", limit)
    best = max(sentences, key=hook_score)
    title = _trim_title(best, limit)

    # Front-load the primary keyphrase only if the title carries none.
    if top and phrases and not (set(_tokens(title)) & top):
        title = _trim_title(f"{_titlecase_phrase(phrases[0])}: {best}", limit)
    return title or "Watch this"


def _seo_description(text: str, phrases: list[str], niche: str, hashtags: list[str]) -> str:
    """A few informative, keyword-rich sentences + CTA + hashtags."""
    lead = _first_sentence(text, 140)
    if lead and lead[0].islower():
        lead = lead[0].upper() + lead[1:]

    covered = [_titlecase_phrase(p) for p in phrases[:3] if " " in p] or \
              [_titlecase_phrase(p) for p in phrases[:3]]
    lines: list[str] = []
    if lead:
        lines.append(lead + ".")
    if covered:
        joined = ", ".join(covered[:-1]) + (" and " + covered[-1] if len(covered) > 1 else covered[0]) \
            if len(covered) > 1 else covered[0]
        lines.append(f"In this clip we break down {joined} — and why it matters.")
    focus = niche or (phrases[0] if phrases else "this topic")
    lines.append(f"Follow for more on {focus}.")
    body = " ".join(lines).strip()
    if hashtags:
        body += "\n\n" + " ".join(hashtags)
    return body


def _analysis_tags(phrases: list[str], niche: str, base_tags: list[str]) -> list[str]:
    """Plain, content-derived tags (unique per clip)."""
    tags: list[str] = []
    for n in niche.split():
        if n:
            tags.append(n.lower())
    for t in base_tags:
        t = t.strip().lstrip("#").lower()
        if t:
            tags.append(t)
    tags += [p for p in phrases]  # keyphrases (already ranked, content-specific)
    # dedupe, keep order, drop 1-char, cap 15
    seen, out = set(), []
    for t in tags:
        t = t.strip()
        if len(t) > 1 and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= 15:
            break
    return out


def _heuristic(clip: Clip, cfg: Config) -> dict[str, Any]:
    text = clip.caption_text or ""
    niche = cfg.get("page.niche") or ""
    base_tags = cfg.get("page.hashtags") or []

    phrases = _keyphrases(text, 12)
    tags = _analysis_tags(phrases, niche, base_tags)

    # Hashtags: platform staples + the top content phrases (hashtag form).
    hashtags: list[str] = ["#shorts", "#reels"]
    for t in tags:
        h = _hashtagify(t)
        if len(h) > 2 and h not in hashtags:
            hashtags.append(h)
        if len(hashtags) >= 8:
            break

    title = _hooked_title(text, phrases)
    description = _seo_description(text, phrases, niche, hashtags)
    first_comment = "What would you add? Drop it in the comments 👇"

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "hashtags": hashtags,
        "first_comment": first_comment,
        "backend": "heuristic",
    }


def _llm_available() -> bool:
    from ..llm import available
    return available()


def _llm(clip: Clip, cfg: Config) -> dict[str, Any]:
    from ..llm import complete_json

    niche = cfg.get("page.niche") or "general"
    tone = cfg.get("page.tone") or "punchy, direct"
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "first_comment": {"type": "string"},
        },
        "required": ["title", "description", "tags", "hashtags", "first_comment"],
        "additionalProperties": False,
    }
    prompt = (
        "You are an SEO copywriter for short-form video (YouTube Shorts, TikTok, "
        "Reels). Write discovery metadata for the clip below. Base everything on "
        "what the clip actually says — it must match this specific clip and read "
        "differently from any other clip.\n\n"
        f"Niche: {niche}. Tone: {tone}.\n\n"
        "Requirements:\n"
        "- title: a scroll-stopping hook UNDER 70 characters, with the clip's "
        "main searchable keyphrase front-loaded. No clickbait that the clip "
        "doesn't deliver.\n"
        "- description: 2-4 informative sentences that tell the viewer and the "
        "algorithm exactly what the clip covers, naturally weaving in the key "
        "search terms, then a short call-to-action. Write to be SEARCHED, not "
        "padded with random words. End with a line of 4-6 relevant hashtags.\n"
        "- tags: 10-15 PLAIN keyword tags (no # symbol) pulled from the actual "
        "content — specific phrases a viewer would search, mixing broad and "
        "long-tail. Analysis-based and unique to this clip, not a genre list.\n"
        "- hashtags: 5-8 hashtags (with #) matching the content.\n"
        "- first_comment: a short engagement CTA.\n\n"
        f"Clip transcript:\n{clip.caption_text}"
    )
    data = complete_json(prompt, schema, max_tokens=1100)
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
