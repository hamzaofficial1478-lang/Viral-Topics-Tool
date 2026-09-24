"""Where a run's files land.

Every clip, sidecar, thumbnail and manifest used to go into one flat ``out/``
folder. After a few dozen links that is unreadable, and the operator asked for
the obvious fix: a folder per source, reused whenever the same link comes back.

The default layout uses **both** identifiers a YouTube link carries::

    out/<uploader>/<video_id>/my-video_en_01_20260901.mp4

so clips from one channel group together *and* each video keeps its own folder
that a re-run lands back in. ``paths.output_template`` changes the shape --
``{video_id}`` alone for a flat list per video, ``{uploader}`` alone to group
only by channel, ``""`` for the old single-folder behaviour.

Windows is the deployment target, and channel names are arbitrary user text, so
``safe_name`` is deliberately strict: names like ``Tom & Jerry: S1/E2`` or a
channel ending in ``.`` are the normal case, not the edge case.
"""

from __future__ import annotations

import os
import re

# Characters Windows forbids in a path component, plus the separators.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Device names Windows still reserves; a folder called "con" cannot be created.
_RESERVED = {"con", "prn", "aux", "nul",
             *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


def safe_name(text: str, fallback: str = "unknown", limit: int = 60) -> str:
    """One path component that Windows will actually accept.

    Trailing dots and spaces matter more than they look: Windows silently
    strips them when creating the directory, so ``"Channel."`` becomes
    ``"Channel"`` on disk while the code keeps looking for the original name --
    a folder that exists and cannot be found.
    """
    s = _ILLEGAL.sub(" ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > limit:
        s = s[:limit].strip(" .")
    if s.lower() in _RESERVED:
        s = f"_{s}"
    return s or fallback


def source_folder(meta, cfg) -> str:
    """Relative sub-path under ``paths.output_dir`` for this source.

    Returns ``""`` when the template is empty or nothing can fill it, which
    keeps the old flat behaviour rather than inventing a folder called
    "unknown".
    """
    template = cfg.get("paths.output_template", "{uploader}/{video_id}")
    if template is None:
        template = "{uploader}/{video_id}"
    template = str(template).strip()
    if not template:
        return ""

    # A local file has no uploader or video id; fall back to its title so the
    # grouping still means something instead of collapsing to one bucket.
    title = getattr(meta, "title", "") or ""
    values = {
        "uploader": safe_name(getattr(meta, "uploader", ""), ""),
        "video_id": safe_name(getattr(meta, "video_id", ""), ""),
        "title": safe_name(title, ""),
    }
    if not values["video_id"] and not values["uploader"]:
        values["video_id"] = values["title"]

    parts: list[str] = []
    for raw in re.split(r"[\\/]+", template):
        if not raw.strip():
            continue
        try:
            filled = raw.format(**values)
        except (KeyError, IndexError):
            # An unknown placeholder is the operator's typo, not a reason to
            # fail a render that has already done all its work.
            filled = raw
        filled = safe_name(filled, "")
        if filled:                      # drop components nothing filled
            parts.append(filled)
    return os.path.join(*parts) if parts else ""


def resolve_output_dir(meta, cfg) -> str:
    """The directory this run's files should be written to (created)."""
    base = cfg.get("paths.output_dir", "out")
    out = os.path.join(base, source_folder(meta, cfg))
    os.makedirs(out, exist_ok=True)
    return out
