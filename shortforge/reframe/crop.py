"""M5 — Reframe to vertical.

Phase 1 is a center smart-crop: crop the source to the target aspect ratio,
centered, then scale to the exact output resolution. (Subject/face tracking to
keep a moving speaker in frame is Phase 2.) A `blur` fill mode is also provided
for sources where cropping would lose too much — it fits the whole frame over a
blurred, zoomed background.

Everything is expressed as an ffmpeg ``-filter_complex`` graph ending in the
named output pad ``[v]`` so the render step can map it directly and optionally
splice in a caption filter.
"""

from __future__ import annotations

from ..utils import ShortForgeError

# Kept identical to render.compose.SCALE_FLAGS on purpose. `encode-sample` --
# the tool the operator uses to JUDGE quality -- builds its graph here, so if
# this scaler differed from production the comparison would be measuring the
# wrong thing. (Engineering rule 6: probe and production share one path.)
_FLAGS = "lanczos"


def parse_aspect(aspect: str, width: int, height: int) -> tuple[int, int]:
    """Resolve an aspect spec to an (out_w, out_h) pixel size.

    Accepts ``"9:16"`` / ``"1:1"`` / ``"16:9"`` (use configured width, derive
    the other side), or an explicit ``"WxH"`` (e.g. ``"1080x1920"``).
    """
    aspect = (aspect or "9:16").strip().lower()
    if "x" in aspect:
        w_s, _, h_s = aspect.partition("x")
        try:
            return _even(int(w_s)), _even(int(h_s))
        except ValueError as e:
            raise ShortForgeError(f"Bad resolution '{aspect}' (want WxH)") from e
    if ":" in aspect:
        a_s, _, b_s = aspect.partition(":")
        try:
            aw, ah = float(a_s), float(b_s)
        except ValueError as e:
            raise ShortForgeError(f"Bad aspect '{aspect}'") from e
        if aw <= 0 or ah <= 0:
            raise ShortForgeError(f"Bad aspect '{aspect}'")
        if ah >= aw:  # portrait or square: anchor on width
            return _even(width), _even(round(width * ah / aw))
        # landscape: anchor on height
        return _even(round(height * aw / ah)), _even(height)
    raise ShortForgeError(f"Unrecognised aspect '{aspect}'")


def _even(n: int) -> int:
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


def compute_crop(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[int, int, int, int]:
    """Center-crop rectangle (cw, ch, x, y) of the source at the target aspect."""
    if src_w <= 0 or src_h <= 0:
        raise ShortForgeError("Invalid source dimensions")
    target_ar = out_w / out_h
    src_ar = src_w / src_h

    if src_ar > target_ar:
        # Source is too wide -> crop the sides.
        cw = _even(round(src_h * target_ar))
        ch = _even(src_h)
        cw = min(cw, src_w)
    else:
        # Source is too tall/narrow -> crop top and bottom.
        cw = _even(src_w)
        ch = _even(round(src_w / target_ar))
        ch = min(ch, src_h)

    x = max(0, (src_w - cw) // 2)
    y = max(0, (src_h - ch) // 2)
    return cw, ch, x, y


def build_filtergraph(
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    fill: str = "crop",
    extra: str | None = None,
) -> str:
    """Build the ``-filter_complex`` string producing ``[v]``.

    Args:
        src_w, src_h: source dimensions.
        out_w, out_h: output dimensions.
        fill: ``crop`` (center smart-crop) or ``blur`` (fit over blurred bg).
        extra: optional filter fragment (e.g. a subtitles filter) appended to
            the video just before the output pad. No leading comma.
    """
    tail = f",{extra}" if extra else ""

    if fill == "blur":
        # Fit the whole frame, centered, over a zoomed + blurred copy of itself.
        return (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase:"
            f"flags={_FLAGS},crop={out_w}:{out_h},gblur=sigma=20[bgb];"
            f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
            f"flags={_FLAGS}[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1{tail}[v]"
        )

    if fill != "crop":
        raise ShortForgeError(f"Unknown fill mode '{fill}' (use crop or blur)")

    cw, ch, x, y = compute_crop(src_w, src_h, out_w, out_h)
    return (
        f"[0:v]crop={cw}:{ch}:{x}:{y},"
        f"scale={out_w}:{out_h}:flags={_FLAGS},setsar=1{tail}[v]"
    )
