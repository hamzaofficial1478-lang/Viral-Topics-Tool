"""M7 — Caption generation as ASS/SSA, burned by ffmpeg's libass filter.

The look is driven by a named template (``captions/templates.py``) so every clip
renders consistently; the animation (none / fade / pop / karaoke / reveal) and
individual fields can be overridden per run. Bottom-anchored by default with a
configurable safe margin so text clears the platform UI.
"""

from __future__ import annotations

import os

from ..config import Config
from ..models import Clip, Transcript, Word
from . import templates


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
    return " ".join(text.replace("\\", "").replace("{", "(").replace("}", ")").split())


def group_word_lines(words: list[Word], max_chars: int, max_duration: float) -> list[list[Word]]:
    """Group words into caption lines (breaks on char or duration limit)."""
    lines: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if not cur:
            cur = [w]
            continue
        candidate_len = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
        span = w.end - cur[0].start
        if candidate_len > max_chars or span > max_duration:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def group_lines(words: list[Word], max_chars: int, max_duration: float):
    """Back-compat: return (start, end, text) tuples."""
    out = []
    for line in group_word_lines(words, max_chars, max_duration):
        text = " ".join(w.text for w in line).strip()
        if text:
            out.append((line[0].start, line[-1].end, text))
    return out


def build_ass(
    clip: Clip,
    transcript: Transcript,
    out_w: int,
    out_h: int,
    cfg: Config,
    out_path: str,
    keep_ranges: list[tuple[float, float]] | None = None,
) -> str | None:
    """Write an ASS file for ``clip``; return its path (or None if no captions).

    When ``keep_ranges`` (M9 jump cuts, source-time) is given, word timings are
    remapped onto the compressed timeline and words that fall entirely in
    trimmed dead air are dropped, so captions stay in sync with the cut video.
    """
    if not cfg.get("captions.enabled", True):
        return None

    words = transcript.words_in(clip.start, clip.end)
    if keep_ranges:
        from ..analyze.audio import remap_time
        rel = [
            Word(
                start=remap_time(w.start, keep_ranges),
                end=remap_time(w.end, keep_ranges),
                text=_escape(w.text),
            )
            for w in words
            if w.text and any(w.end > s and w.start < e for s, e in keep_ranges)
        ]
    else:
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

    lines = [
        ln
        for ln in group_word_lines(
            rel,
            int(cfg.get("captions.max_line_chars", 30)),
            float(cfg.get("captions.max_line_duration", 2.5)),
        )
        if ln
    ]
    if not lines:
        return None

    style = templates.resolve(cfg)
    upper = bool(style.get("uppercase"))
    anim = style["animation"]

    header = _ass_header(out_w, out_h, style)
    if anim == "karaoke":
        events = [_karaoke_event(ln, upper) for ln in lines]
    elif anim == "reveal":
        events = [_reveal_event(ln, upper) for ln in lines]
    else:
        prefix = _anim_prefix(anim)
        events = [_simple_event(ln, upper, prefix) for ln in lines]
    content = header + "\n".join(events) + "\n"

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path


def _txt(text: str, upper: bool) -> str:
    return text.upper() if upper else text


def _anim_prefix(anim: str) -> str:
    if anim == "fade":
        return r"{\fad(120,60)}"
    if anim == "pop":
        return r"{\fscx55\fscy55\fad(60,30)\t(0,150,\fscx100\fscy100)}"
    return ""  # none


def _dialogue(start: float, end: float, body: str) -> str:
    return f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{body}"


def _simple_event(line: list[Word], upper: bool = False, prefix: str = "") -> str:
    text = _txt(" ".join(w.text for w in line).strip(), upper)
    return _dialogue(line[0].start, line[-1].end, prefix + text)


def _karaoke_event(line: list[Word], upper: bool = False, fade: bool = True) -> str:
    parts: list[str] = []
    prev_end = line[0].start
    for w in line:
        # Fill spans from the previous word's end to this word's end, so each
        # word is fully highlighted exactly when it finishes being said.
        dur_cs = max(1, int(round((w.end - prev_end) * 100)))
        parts.append(f"{{\\k{dur_cs}}}{_txt(w.text, upper)} ")
        prev_end = w.end
    prefix = r"{\fad(80,40)}" if fade else ""
    return _dialogue(line[0].start, line[-1].end, prefix + "".join(parts).strip())


def _reveal_event(line: list[Word], upper: bool = False) -> str:
    start, end = line[0].start, line[-1].end
    parts: list[str] = []
    for w in line:
        r0 = int(round((w.start - start) * 1000))
        parts.append(f"{{\\alpha&HFF&\\t({r0},{r0 + 90},\\alpha&H00&)}}{_txt(w.text, upper)} ")
    return _dialogue(start, end, "".join(parts).strip())


def _ass_header(out_w: int, out_h: int, style: dict) -> str:
    karaoke = style.get("animation") == "karaoke"
    # Karaoke: PrimaryColour is the sung colour, SecondaryColour the unsung one.
    primary = style["highlight_color"] if karaoke else style["primary_color"]
    secondary = style["primary_color"]
    bold = -1 if style.get("bold", 1) else 0
    side = max(40, out_w // 12)

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
        f"Style: Default,{style['font']},{int(style['font_size'])},{primary},"
        f"{secondary},{style['outline_color']},{style['back_color']},{bold},0,0,0,"
        f"100,100,0,0,{int(style['border_style'])},{int(style['outline'])},"
        f"{int(style['shadow'])},{int(style['alignment'])},{side},{side},"
        f"{int(style['bottom_margin'])},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )


def subtitles_filter(ass_path: str, fontsdir: str | None = None) -> str:
    esc = ass_path.replace("\\", "\\\\").replace("'", r"\'")
    frag = f"subtitles=filename='{esc}'"
    if fontsdir and os.path.isdir(fontsdir):
        frag += f":fontsdir='{fontsdir}'"
    return frag
