"""Caption templates (M7 presets) — named, reusable caption looks.

A *template* fixes the caption look (font, colours, position, box, default
animation) so every clip on a page renders consistently. The operator picks a
template by name and can still override individual fields (font, colour,
animation, uppercase) from config or the CLI.

Colours are ASS ``&HAABBGGRR`` (alpha 00 = opaque, FF = transparent).

Animations (``animation``):
  none    - static
  fade    - fade the line in/out
  pop     - scale-in "pop" (+ quick fade)
  karaoke - word-by-word highlight fill (sung word takes highlight_color)
  reveal  - words appear one-by-one as spoken
"""

from __future__ import annotations

from ..config import Config

WHITE = "&H00FFFFFF"
BLACK = "&H00000000"
AMBER = "&H0000E5FF"
YELLOW = "&H0000FFFF"
GREEN = "&H0000FF00"
BOX_DARK = "&H90000000"

# Every template carries a full set of keys so the ASS writer is fully data-driven.
_BASE = {
    "font": "DejaVu Sans",
    "font_size": 54,
    "bold": 1,
    "primary_color": WHITE,      # unsung / base text
    "highlight_color": AMBER,    # sung word (karaoke/reveal)
    "outline_color": BLACK,
    "back_color": "&H64000000",
    "border_style": 1,           # 1 = outline+shadow, 3 = opaque box
    "outline": 3,
    "shadow": 1,
    "alignment": 2,              # 2 = bottom-center
    "bottom_margin": 320,
    "uppercase": False,
    "animation": "fade",
}


def _tpl(**over):
    d = dict(_BASE)
    d.update(over)
    return d


TEMPLATES: dict[str, dict] = {
    # Neutral, readable — good default.
    "clean": _tpl(animation="fade"),
    # Big, uppercase, scale-in pop. Punchy.
    "bold_pop": _tpl(font_size=62, uppercase=True, outline=4, animation="pop"),
    # Classic amber karaoke fill.
    "karaoke_amber": _tpl(font_size=58, highlight_color=AMBER, animation="karaoke"),
    # Word-by-word reveal with a green accent, uppercase.
    "reveal_green": _tpl(
        font_size=60, uppercase=True, highlight_color=GREEN, outline=4, animation="reveal"
    ),
    # Text sits in a filled box (high legibility over busy footage).
    "boxed": _tpl(border_style=3, back_color=BOX_DARK, outline=8, shadow=0, animation="fade"),
    # Small, subtle, no outline.
    "minimal": _tpl(font_size=46, outline=2, shadow=0, animation="none"),
}

ANIMATIONS = ["none", "fade", "pop", "karaoke", "reveal"]
_STYLE_TO_ANIM = {"karaoke": "karaoke", "simple": "fade"}

# Caption config keys that, when set to a non-null value, override the template.
_OVERRIDABLE = [
    "font", "font_size", "primary_color", "highlight_color", "outline_color",
    "outline", "shadow", "bottom_margin", "uppercase", "animation",
]


def list_templates() -> list[str]:
    return list(TEMPLATES.keys())


def resolve(cfg: Config) -> dict:
    """Resolve the effective caption look: template + explicit overrides."""
    name = cfg.get("captions.template", "clean")
    style = TEMPLATES.get(name, TEMPLATES["clean"]).copy()

    for key in _OVERRIDABLE:
        val = cfg.get(f"captions.{key}")
        if val is not None:
            style[key] = val

    # Back-compat: captions.style (karaoke|simple) sets animation if not given.
    if cfg.get("captions.animation") is None:
        legacy = cfg.get("captions.style")
        if legacy in _STYLE_TO_ANIM:
            style["animation"] = _STYLE_TO_ANIM[legacy]

    if style.get("animation") not in ANIMATIONS:
        style["animation"] = "fade"
    style["_template"] = name
    return style
