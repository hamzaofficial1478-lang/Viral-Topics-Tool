"""M7 caption templates + animations."""

import os

from shortforge.captions import build_ass
from shortforge.captions.templates import list_templates, resolve
from shortforge.config import Config
from shortforge.models import Clip


def _cfg(**over):
    c = Config.load()
    for k, v in over.items():
        c.override(k, v)
    return c


def test_templates_exist():
    names = list_templates()
    assert "clean" in names and "bold_pop" in names and len(names) >= 5


def test_resolve_default_is_clean_fade():
    st = resolve(_cfg())
    assert st["_template"] == "clean"
    assert st["animation"] == "fade"


def test_resolve_template_selects_preset():
    st = resolve(_cfg(**{"captions.template": "bold_pop"}))
    assert st["animation"] == "pop"
    assert st["uppercase"] is True


def test_resolve_explicit_override_wins():
    st = resolve(_cfg(**{"captions.template": "clean", "captions.animation": "reveal",
                         "captions.font": "DejaVu Serif"}))
    assert st["animation"] == "reveal"
    assert st["font"] == "DejaVu Serif"


def test_resolve_legacy_style_maps_to_animation():
    st = resolve(_cfg(**{"captions.style": "karaoke"}))
    assert st["animation"] == "karaoke"


def _clip(t):
    return Clip("01", "h", t.segments[1].start, t.segments[2].end, 0.8)


def test_build_ass_pop_has_scale_tag(tmp_path, sample_transcript):
    cfg = _cfg(**{"captions.template": "bold_pop"})
    out = str(tmp_path / "c.ass")
    build_ass(_clip(sample_transcript), sample_transcript, 1080, 1920, cfg, out)
    body = open(out, encoding="utf-8").read()
    assert "\\fscx" in body            # scale-in pop
    assert body.split("Default,,")[0]  # style header present


def test_build_ass_reveal_has_alpha_tags(tmp_path, sample_transcript):
    cfg = _cfg(**{"captions.template": "reveal_green"})
    out = str(tmp_path / "c.ass")
    build_ass(_clip(sample_transcript), sample_transcript, 1080, 1920, cfg, out)
    body = open(out, encoding="utf-8").read()
    assert "\\alpha&HFF&" in body       # per-word reveal
    # reveal_green is uppercase — a caption word should appear upper-cased.
    assert "BIGGEST" in body and "biggest" not in body


def test_boxed_template_uses_opaque_box(tmp_path, sample_transcript):
    cfg = _cfg(**{"captions.template": "boxed"})
    out = str(tmp_path / "c.ass")
    build_ass(_clip(sample_transcript), sample_transcript, 1080, 1920, cfg, out)
    body = open(out, encoding="utf-8").read()
    # BorderStyle 3 = opaque box (field index 15 in the V4+ Style order).
    style_line = next(ln for ln in body.splitlines() if ln.startswith("Style: Default"))
    fields = style_line.split(",")
    assert fields[15] == "3"
