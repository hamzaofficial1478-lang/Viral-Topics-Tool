"""Build the full ffmpeg filter_complex for a clip.

Centralises the video graph so both render paths share it:
  - plain path: input [0:v] is the full source frame -> crop/scale -> captions -> logo
  - tracked path: input [0:v] is already cropped (OpenCV) -> captions -> logo

Ends in the pad ``[v]``.
"""

from __future__ import annotations

from typing import Any

from ..reframe.crop import compute_crop


def _esc(path: str) -> str:
    return path.replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")


def build_filtergraph(
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    *,
    fill: str = "crop",
    pre_cropped: bool = False,
    subtitles: str | None = None,
    logo: dict[str, Any] | None = None,
) -> str:
    nodes: list[str] = []
    cur = "0:v"

    if not pre_cropped:
        if fill == "blur":
            nodes.append(
                f"[0:v]split=2[bg][fg];"
                f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                f"crop={out_w}:{out_h},gblur=sigma=20[bgb];"
                f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[ref]"
            )
        else:
            cw, ch, x, y = compute_crop(src_w, src_h, out_w, out_h)
            nodes.append(
                f"[0:v]crop={cw}:{ch}:{x}:{y},scale={out_w}:{out_h},setsar=1[ref]"
            )
        cur = "ref"

    if subtitles:
        nodes.append(f"[{cur}]{subtitles}[cap]")
        cur = "cap"

    if logo:
        w = int(logo.get("width", out_w // 5))
        alpha = float(logo.get("alpha", 0.85))
        lx = logo.get("x", "W-w-40")
        ly = logo.get("y", "40")
        nodes.append(
            f"movie='{_esc(logo['path'])}',format=rgba,"
            f"colorchannelmixer=aa={alpha},scale={w}:-1[lg]"
        )
        nodes.append(f"[{cur}][lg]overlay={lx}:{ly}[v]")
        cur = "v"

    if cur != "v":
        nodes.append(f"[{cur}]null[v]")
    return ";".join(nodes)


def logo_position(corner: str, out_w: int, out_h: int, margin: int = 40) -> tuple[str, str]:
    """Overlay x/y expressions for a corner (TL/TR/BL/BR)."""
    corner = (corner or "TR").upper()
    x = f"{margin}" if corner in ("TL", "BL") else "W-w-%d" % margin
    y = f"{margin}" if corner in ("TL", "TR") else "H-h-%d" % margin
    return x, y
