"""A3: single caption layer + burned-in source-text treatment."""

from shortforge.captions import burned_in
from shortforge.render.compose import build_filtergraph


def test_exactly_one_caption_layer():
    fg = build_filtergraph(1920, 1080, 1080, 1920, subtitles="subtitles=one.ass")
    # Exactly one subtitles filter is composited, never stacked.
    assert fg.count("subtitles=") == 1


def test_no_caption_layer_when_none():
    fg = build_filtergraph(1920, 1080, 1080, 1920)
    assert "subtitles=" not in fg


def test_cover_subgraph_draws_box():
    band = {"y": 900, "h": 150, "where": "bottom"}
    frag = burned_in.subgraph("0:v", "bt", band, "cover")
    assert "drawbox=x=0:y=900:w=iw:h=150" in frag
    assert frag.endswith("[bt]")


def test_blur_subgraph_uses_crop_and_boxblur():
    band = {"y": 900, "h": 150, "where": "bottom"}
    frag = burned_in.subgraph("0:v", "bt", band, "blur")
    assert "boxblur" in frag and "crop=iw:150:0:900" in frag
    assert frag.endswith("[bt]")


def test_crop_subgraph_trims_band_by_edge():
    bottom = burned_in.subgraph("0:v", "bt", {"y": 900, "h": 150, "where": "bottom"}, "crop")
    top = burned_in.subgraph("0:v", "bt", {"y": 0, "h": 150, "where": "top"}, "crop")
    assert "crop=iw:ih-150:0:0" in bottom      # trim bottom band
    assert "crop=iw:ih-150:0:150" in top       # trim top band


def test_filtergraph_applies_burned_treatment_before_crop():
    band = {"y": 900, "h": 150, "where": "bottom"}
    fg = build_filtergraph(1920, 1080, 1080, 1920, fill="crop",
                           subtitles="subtitles=x.ass", burned_band=band, burned_mode="cover")
    # The treated label [bt] feeds the crop; our caption still composited once.
    assert "[bt]crop=" in fg
    assert fg.count("subtitles=") == 1
    assert fg.strip().endswith("[v]")


def test_burned_treatment_skipped_when_pre_cropped():
    band = {"y": 900, "h": 150, "where": "bottom"}
    fg = build_filtergraph(1080, 1920, 1080, 1920, pre_cropped=True,
                           subtitles="subtitles=x.ass", burned_band=band)
    # Tracked path treats frames in the render loop, not the filtergraph.
    assert "drawbox" not in fg and "[bt]" not in fg
