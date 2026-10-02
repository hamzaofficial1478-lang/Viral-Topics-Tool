"""M14 — Publishing sidecars (lean Phase 4).

Every rendered clip gets a companion text file with the Title, Description, and
Tags, ready to copy-paste into YouTube / TikTok / Reels. The same fields live in
the manifest, but the sidecar puts them right next to the video so the operator
never has to dig through JSON at upload time.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..utils import log

_HASHTAG_LINE = re.compile(r"^\s*(#\S+\s*)+$")


def _clean_description(description: str) -> str:
    """Drop a trailing hashtag-only block so tags aren't shown twice."""
    lines = (description or "").rstrip().splitlines()
    while lines and (_HASHTAG_LINE.match(lines[-1]) or not lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


def sidecar_text(md: dict[str, Any]) -> str:
    """Render the human, copy-paste-ready title/description/tags block."""
    title = (md.get("title") or "").strip()
    description = _clean_description(md.get("description") or "")
    tags = md.get("tags") or []                 # plain keywords (YouTube tag field)
    hashtags = md.get("hashtags") or []         # hashtag form (Shorts/TikTok/Reels)
    first = (md.get("first_comment") or "").strip()

    parts = [
        "TITLE",
        title or "(none)",
        "",
        "DESCRIPTION",
        description or "(none)",
        "",
        "TAGS (comma-separated, e.g. YouTube tag field)",
        ", ".join(tags) if tags else "(none)",
        "",
        "HASHTAGS (captions / Shorts / TikTok / Reels)",
        " ".join(hashtags) if hashtags else "(none)",
    ]
    if first:
        parts += ["", "FIRST COMMENT", first]
    return "\n".join(parts) + "\n"


def write_sidecar(video_path: str, md: dict[str, Any] | None) -> str | None:
    """Write ``<video>.txt`` next to the clip. Returns the path (or None)."""
    if not md:
        return None
    base = os.path.splitext(video_path)[0]
    out_path = f"{base}.txt"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(sidecar_text(md))
    except OSError as e:  # never let a sidecar failure sink a render
        log.warning("could not write publishing sidecar for %s: %s", video_path, e)
        return None
    return out_path


def write_index(index: dict[str, Any], out_dir: str, name: str) -> str:
    """Write a small cross-run/batch index JSON. Returns the path."""
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return path


__all__ = ["sidecar_text", "write_sidecar", "write_index"]
