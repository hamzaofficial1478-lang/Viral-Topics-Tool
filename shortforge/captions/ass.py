"""M7 — Caption generation as ASS/SSA, burned by ffmpeg's libass filter.

Two styles, selected by ``captions.style``:
  - "simple"  : Phase-1 readable bottom-anchored lines.
  - "karaoke" : word-by-word highlight — each word fills to a highlight colour as
                it is spoken (proven to lift short-form retention).

Both are bottom-anchored with a configurable safe margin so text clears the
platform action bar / right-side button rail.
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
) -> str | None:
    """Write an ASS file for ``clip``; return its path (or None if no captions)."""
    if not cfg.get("captions.enabled", True):
        return None

    words = transcript.words_in(clip.start, clip.end)
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

    lines = group_word_lines(
        rel,
        int(cfg.get("captions.max_line_chars", 30)),
        float(cfg.get("captions.max_line_duration", 2.5)),
    )
    lines = [ln for ln in lines if ln]
    if not lines:
        return None

    karaoke = str(cfg.get("captions.style", "karaoke")).lower() == "karaoke"
    header = _ass_header(out_w, out_h, cfg, karaoke)
    if karaoke:
        events = "\n".join(_karaoke_event(ln) for ln in lines)
    else:
        events = "\n".join(_simple_event(ln) for ln in lines)
    content = header + events + "\n"

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path


def _simple_event(line: list[Word]) -> str:
    start, end = line[0].start, line[-1].end
    text = " ".join(w.text for w in line).strip()
    return f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"


def _karaoke_event(line: list[Word]) -> str:
    start, end = line[0].start, line[-1].end
    parts: list[str] = []
    prev_end = start
    for w in line:
        # Fill duration spans from the previous word's end to this word's end,
        # so each word is fully highlighted exactly when it finishes being said.
        dur_cs = max(1, int(round((w.end - prev_end) * 100)))
        parts.append(f"{{\\k{dur_cs}}}{w.text} ")
        prev_end = w.end
    text = "".join(parts).strip()
    return f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"


def _ass_header(out_w: int, out_h: int, cfg: Config, karaoke: bool) -> str:
    font = cfg.get("captions.font", "DejaVu Sans")
    size = int(cfg.get("captions.font_size", 54))
    base = cfg.get("captions.primary_color", "&H00FFFFFF")           # unsung/white
    highlight = cfg.get("captions.highlight_color", "&H0000E5FF")     # sung (amber)
    outline_c = cfg.get("captions.outline_color", "&H00000000")
    outline = int(cfg.get("captions.outline", 3))
    shadow = int(cfg.get("captions.shadow", 1))
    margin_v = int(cfg.get("captions.bottom_margin", 320))
    side = max(40, out_w // 12)

    # Karaoke: PrimaryColour is the highlighted (sung) colour, SecondaryColour is
    # the not-yet-sung colour. Simple: both the same.
    primary = highlight if karaoke else base
    secondary = base

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
        f"Style: Default,{font},{size},{primary},{secondary},{outline_c},"
        f"&H64000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,"
        f"{side},{side},{margin_v},1\n\n"
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
