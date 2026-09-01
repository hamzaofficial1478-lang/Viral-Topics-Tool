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


# ffmpeg's scale filter defaults to bicubic. Lanczos resolves noticeably more
# fine detail on the enlargement this pipeline nearly always has to do, for a
# few percent of render time.
SCALE_FLAGS = "lanczos"

# Applied only when the crop is being enlarged. Upscaling cannot add detail,
# but it does soften every edge that was there; a mild unsharp mask restores
# the apparent crispness without the halos a heavier setting produces.
# luma 5x5, amount 0.8 -- deliberately conservative, since these clips get
# re-encoded again by the platform and over-sharpening turns into ringing.
SHARPEN = "unsharp=5:5:0.8:3:3:0.4"


def _scale(out_w: int, out_h: int, sharpen: bool) -> str:
    s = f"scale={out_w}:{out_h}:flags={SCALE_FLAGS}"
    return f"{s},{SHARPEN}" if sharpen else s


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
    video_select: str | None = None,
    burned_band: dict | None = None,
    burned_mode: str = "cover",
    sharpen: bool = False,
) -> str:
    nodes: list[str] = []
    vin = "0:v"

    # M9 jump cuts (plain path): keep only the speech ranges, then re-stamp
    # PTS so the removed dead air closes up. Captions are remapped to match.
    if video_select and not pre_cropped:
        nodes.append(f"[0:v]select='{video_select}',setpts=N/FRAME_RATE/TB[jc]")
        vin = "jc"

    # A3: treat burned-in source captions (cover/blur/crop) before compositing
    # ours, so the source text can't remain on screen under our captions.
    if burned_band and not pre_cropped:
        from ..captions import burned_in
        nodes.append(burned_in.subgraph(vin, "bt", burned_band, burned_mode))
        vin = "bt"
    cur = vin

    if not pre_cropped:
        if fill == "blur":
            nodes.append(
                f"[{vin}]split=2[bg][fg];"
                f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase:"
                f"flags={SCALE_FLAGS},crop={out_w}:{out_h},gblur=sigma=20[bgb];"
                f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
                f"flags={SCALE_FLAGS}[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[ref]"
            )
        else:
            cw, ch, x, y = compute_crop(src_w, src_h, out_w, out_h)
            nodes.append(
                f"[{vin}]crop={cw}:{ch}:{x}:{y},"
                f"{_scale(out_w, out_h, sharpen)},setsar=1[ref]"
            )
        cur = "ref"
    elif sharpen:
        # Tracked path: OpenCV already cropped and resized, so the softening
        # has happened upstream -- sharpen here instead of skipping it.
        nodes.append(f"[{cur}]{SHARPEN}[shp]")
        cur = "shp"

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
