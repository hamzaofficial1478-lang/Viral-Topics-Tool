"""Phase 2: filtergraph composition (M8) + metadata (M10) + karaoke (M7)."""

from shortforge.config import Config
from shortforge.metadata import generate
from shortforge.models import Clip
from shortforge.render.compose import build_filtergraph, logo_position


def test_compose_plain_crop_captions():
    fg = build_filtergraph(1920, 1080, 1080, 1920, subtitles="subtitles=x.ass")
    assert "[0:v]crop=" in fg
    assert "subtitles=x.ass" in fg
    assert fg.endswith("[v]")


def test_compose_precropped_skips_crop():
    fg = build_filtergraph(1080, 1920, 1080, 1920, pre_cropped=True, subtitles="subtitles=x.ass")
    assert "crop=" not in fg
    assert fg.endswith("[v]")


def test_compose_with_logo():
    logo = {"path": "/tmp/logo.png", "x": "W-w-40", "y": "40", "width": 200, "alpha": 0.8}
    fg = build_filtergraph(1920, 1080, 1080, 1920, logo=logo)
    assert "movie='/tmp/logo.png'" in fg
    assert "overlay=W-w-40:40" in fg
    assert fg.endswith("[v]")


def test_logo_position_corners():
    assert logo_position("TL", 1080, 1920)[0] == "40"
    assert logo_position("BR", 1080, 1920) == ("W-w-40", "H-h-40")


def test_metadata_heuristic():
    clip = Clip("01", "h", 0.0, 30.0, 0.9,
                caption_text="The biggest mistake founders make is quitting too early. "
                             "Here is why persistence beats talent every time.")
    cfg = Config.load()
    cfg.override("metadata.backend", "heuristic")
    cfg.override("page.niche", "startups")
    md = generate(clip, cfg)
    assert md["title"]
    assert 1 <= len(md["hashtags"]) <= 8
    assert all(h.startswith("#") for h in md["hashtags"])
    assert any("startup" in h for h in md["hashtags"])
    # Analysis-based plain tags exist and are not hashtags.
    assert md["tags"] and all(not t.startswith("#") for t in md["tags"])
    # Description is detailed (more than one sentence) and carries a CTA.
    assert md["description"].count(".") >= 2
    assert "Follow for more" in md["description"]
    assert md["backend"] == "heuristic"


def test_metadata_tags_are_content_specific_per_clip():
    cfg = Config.load()
    cfg.override("metadata.backend", "heuristic")
    a = generate(Clip("01", "h", 0, 30, 0.9,
                 caption_text="Compound interest is how small savings grow into wealth "
                              "over decades of patient investing in index funds."), cfg)
    b = generate(Clip("02", "h", 0, 30, 0.9,
                 caption_text="Cast iron pans need seasoning with oil and heat so the "
                              "surface becomes naturally nonstick for cooking eggs."), cfg)
    # Tags reflect each clip's actual content, so the two sets differ clearly.
    assert set(a["tags"]) != set(b["tags"])
    assert any("interest" in t or "investing" in t or "index" in t for t in a["tags"])
    assert any("cast iron" in t or "seasoning" in t or "nonstick" in t for t in b["tags"])


def test_karaoke_event_has_k_tags():
    from shortforge.captions.ass import _karaoke_event
    from shortforge.models import Word
    line = [Word(0.0, 0.4, "did"), Word(0.4, 0.8, "you"), Word(0.8, 1.2, "know")]
    ev = _karaoke_event(line)
    assert ev.count("\\k") == 3   # one karaoke tag per word
    assert "did" in ev and "know" in ev
