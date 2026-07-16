"""M11 — Thumbnail / cover (Phase 2).

Picks an expressive keyframe from the clip and exports it at the output aspect.
If tracking is available, the frame is chosen where the speaker is most centred
(a decent proxy for "expressive"); otherwise a frame a fifth of the way in.
An optional short text hook is drawn on top when a font is available.
"""

from __future__ import annotations

import os

from ..config import Config
from ..models import Clip
from ..render.compose import build_filtergraph
from ..utils import ShortForgeError, require_binary, run, format_timestamp, log

_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _pick_time(clip: Clip, track) -> float:
    """Choose a keyframe time (source seconds)."""
    if track is not None and track.times:
        # Frame where the subject sits closest to the source centre.
        cx0 = track.src_w / 2.0
        best_t, best_d = track.times[0], 1e18
        for t, cx in zip(track.times, track.cx):
            d = abs(cx - cx0)
            if d < best_d:
                best_d, best_t = d, t
        return clip.start + best_t
    return clip.start + 0.2 * clip.duration


def _font() -> str | None:
    for f in _FONTS:
        if os.path.isfile(f):
            return f
    return None


def _drawtext(text: str, out_h: int) -> str | None:
    font = _font()
    if not font or not text:
        return None
    safe = text.replace("\\", "").replace(":", r"\:").replace("'", r"’").replace("%", "")
    safe = safe[:40]
    size = max(36, out_h // 22)
    y = int(out_h * 0.12)
    return (
        f"drawtext=fontfile='{font}':text='{safe}':fontcolor=white:fontsize={size}:"
        f"box=1:boxcolor=black@0.5:boxborderw=18:x=(w-text_w)/2:y={y}"
    )


def make_thumbnail(
    source_path: str,
    clip: Clip,
    out_w: int,
    out_h: int,
    cfg: Config,
    out_path: str,
    track=None,
    text_hook: str | None = None,
) -> str | None:
    """Export a cover image for ``clip``. Returns the path (or None on failure)."""
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    t = _pick_time(clip, track)
    fill = cfg.get("reframe.fill", "crop")
    src_w = int(cfg.get("reframe._src_w", 0)) or 0
    src_h = int(cfg.get("reframe._src_h", 0)) or 0

    graph = build_filtergraph(
        src_w or out_w, src_h or out_h, out_w, out_h, fill=fill
    )
    # Optional text hook: splice a drawtext node before [v].
    if cfg.get("thumbnail.text_hook", False):
        dt = _drawtext(text_hook or clip.caption_text, out_h)
        if dt:
            graph = graph.replace("[v]", f",{dt}[v]", 1) if graph.endswith("[v]") else graph

    cmd = [
        ffmpeg, "-y",
        "-ss", format_timestamp(t),
        "-i", source_path,
        "-frames:v", "1",
        "-filter_complex", graph,
        "-map", "[v]",
        out_path,
    ]
    try:
        run(cmd)
    except ShortForgeError as e:
        log.warning("thumbnail failed for clip %s: %s", clip.clip_id, e)
        return None
    log.info("thumbnail clip %s -> %s", clip.clip_id, out_path)
    return out_path
