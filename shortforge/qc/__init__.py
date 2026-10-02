"""M12 — QC / human review gate (Phase 3, lightweight).

Phase 1-3 has no UI, so the review gate is expressed in the manifest: every clip
is marked ``pending_review`` with the facts a human needs to approve it (the
caption/translation text, dub method, and any brand-safety flags). The operator
approves before anything is scheduled/published (Phase 5).
"""

from __future__ import annotations

import re

# Small, conservative advertiser-friendliness list (English). Matched by prefix
# so inflections are caught ("fuck" -> "fucking"); roots are curated to avoid
# common false positives. Real deployments should add a per-language list.
_ROOTS = (
    "fuck", "motherfuck", "shit", "bitch", "asshole", "bastard", "cunt",
    "slut", "nigger", "faggot", "whore", "rape",
)
_PHRASES = ("kill yourself", "suicide")
_WORD = re.compile(r"[a-z']+")


def brand_safety_flags(text: str) -> list[str]:
    low = (text or "").lower()
    tokens = _WORD.findall(low)
    hits = sorted({r for t in tokens for r in _ROOTS if t == r or t.startswith(r)})
    flags = []
    if hits:
        flags.append(f"profanity/risky: {', '.join(hits[:5])}")
    for phrase in _PHRASES:
        if phrase in low:
            flags.append(f"sensitive: {phrase}")
    return flags


def review_clip(caption_text: str, language: str, dub_method: str) -> dict:
    """Build the per-clip QC record."""
    flags = brand_safety_flags(caption_text)
    return {
        "status": "flagged" if flags else "pending_review",
        "language": language,
        "dub_method": dub_method,
        "flags": flags,
    }
