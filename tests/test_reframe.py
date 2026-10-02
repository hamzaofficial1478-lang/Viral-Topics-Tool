"""M5 reframe geometry."""

import pytest

from shortforge.reframe import compute_crop, build_filtergraph, parse_aspect
from shortforge.utils import ShortForgeError


def test_parse_aspect_ratios():
    assert parse_aspect("9:16", 1080, 1920) == (1080, 1920)
    assert parse_aspect("1:1", 1080, 1920) == (1080, 1080)
    # Landscape anchors on height; width is even-rounded.
    w, h = parse_aspect("16:9", 1080, 1920)
    assert h == 1920 and w % 2 == 0 and w > h


def test_parse_aspect_explicit_wxh():
    assert parse_aspect("720x1280", 1080, 1920) == (720, 1280)


def test_parse_aspect_rejects_garbage():
    with pytest.raises(ShortForgeError):
        parse_aspect("banana", 1080, 1920)


def test_crop_from_landscape_to_vertical():
    # 1920x1080 (16:9) -> 9:16: should crop the sides, keep full height.
    cw, ch, x, y = compute_crop(1920, 1080, 1080, 1920)
    assert ch == 1080
    assert cw < 1920
    assert y == 0
    assert x == (1920 - cw) // 2
    # cropped region matches the 9:16 aspect.
    assert abs((cw / ch) - (1080 / 1920)) < 0.02
    assert cw % 2 == 0 and ch % 2 == 0


def test_crop_never_exceeds_source():
    cw, ch, x, y = compute_crop(640, 480, 1080, 1920)
    assert cw <= 640 and ch <= 480
    assert x >= 0 and y >= 0


def test_filtergraph_shapes():
    crop_fg = build_filtergraph(1920, 1080, 1080, 1920, "crop")
    assert crop_fg.startswith("[0:v]crop=") and crop_fg.endswith("[v]")

    blur_fg = build_filtergraph(1920, 1080, 1080, 1920, "blur")
    assert "gblur" in blur_fg and "overlay" in blur_fg and blur_fg.endswith("[v]")

    with_caps = build_filtergraph(1920, 1080, 1080, 1920, "crop", extra="subtitles=x.ass")
    assert ",subtitles=x.ass[v]" in with_caps
