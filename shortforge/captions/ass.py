"""M7 — Caption generation as ASS/SSA, burned by ffmpeg's libass filter.

Phase 1 renders simple, readable, bottom-anchored captions grouped a few words
at a time from the (source) word timing. Styling and a bottom safe-margin come
from ``captions.*`` in config, so text clears the platform action bar and the
right-side button rail. (Word-by-word karaoke highlight is a Phase 2 upgrade.)
"""

from __future__ import annotations

import os

from ..config import Config
from ..models import Clip, Transcript, Word


def _ass_time(seconds: float) -> str:
    """Seconds -> ASS timestamp H:MM:SS.cc (centiseconds)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs == 100:
        cs = 0
        s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    # Keep libass from interpreting override blocks; collapse whitespace.
    return " ".join(text.replace("\\", "").replace("{", "(").replace("}", ")").split())


def group_lines(
    words: list[Word],
    max_chars: int,
    max_duration: float,
) -> list[tuple[float, float, str]]:
    """Group words into caption lines. Returns (start, end, text) tuples.

    A line breaks when it would exceed ``max_chars`` or span more than
    ``max_duration`` seconds.
    """
    lines: list[tuple[float, float, str]] = []
    cur: list[Word] = []

    def flush() -> None:
        if cur:
            text = " ".join(w.text for w in cur).strip()
            if text:
                lines.append((cur[0].start, cur[-1].end, text))

    for w in words:
        if not cur:
            cur = [w]
            continue
        candidate_len = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
        span = w.end - cur[0].start
        if candidate_len > max_chars or span > max_duration:
            flush()
            cur = [w]
        else:
            cur.append(w)
    flush()
    return lines


def build_ass(
    clip: Clip,
    transcript: Transcript,
    out_w: int,
    out_h: int,
    cfg: Config,
    out_path: str,
) -> str | None:
    """Write an ASS file for ``clip``; return its path (or None if no captions).

    Times are made relative to the clip start (the render step seeks to
    ``clip.start``, so clip time begins at 0).
    """
    if not cfg.get("captions.enabled", True):
        return None

    words = transcript.words_in(clip.start, clip.end)
    # Re-base word timings to the clip.
    rel = [
        Word(
            start=max(0.0, w.start - clip.start),
            end=max(0.0, w.end - clip.start),
            text=_escape(w.text),
        )
        for w in words
        if w.text
    ]
    if not rel:
        return None

    lines = group_lines(
        rel,
        int(cfg.get("captions.max_line_chars", 30)),
        float(cfg.get("captions.max_line_duration", 2.5)),
    )
    if not lines:
        return None

    header = _ass_header(out_w, out_h, cfg)
    events = "\n".join(
        f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Default,,0,0,0,,{text}"
        for (s, e, text) in lines
    )
    content = header + events + "\n"

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path


def _ass_header(out_w: int, out_h: int, cfg: Config) -> str:
    font = cfg.get("captions.font", "DejaVu Sans")
    size = int(cfg.get("captions.font_size", 54))
    primary = cfg.get("captions.primary_color", "&H00FFFFFF")
    outline_c = cfg.get("captions.outline_color", "&H00000000")
    outline = int(cfg.get("captions.outline", 3))
    shadow = int(cfg.get("captions.shadow", 1))
    margin_v = int(cfg.get("captions.bottom_margin", 320))
    side = max(40, out_w // 12)

    # Alignment 2 = bottom-center; MarginV is measured from the bottom edge, so
    # a large value lifts captions above the platform UI safe zone.
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {out_w}\n"
        f"PlayResY: {out_h}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{primary},{primary},{outline_c},"
        f"&H64000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,"
        f"{side},{side},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )


def subtitles_filter(ass_path: str, fontsdir: str | None = None) -> str:
    """Build the ffmpeg subtitles filter fragment for ``ass_path``."""
    esc = ass_path.replace("\\", "\\\\").replace("'", r"\'")
    frag = f"subtitles=filename='{esc}'"
    if fontsdir and os.path.isdir(fontsdir):
        frag += f":fontsdir='{fontsdir}'"
    return frag
