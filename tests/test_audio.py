"""M9 auto-edit audio: loudnorm + silence-trim planning."""

from shortforge.analyze.audio import (
    loudnorm_filter,
    plan_keep_ranges,
    remap_time,
    remap_words,
    select_expr,
    total_kept,
)
from shortforge.config import Config
from shortforge.models import Word


def test_loudnorm_filter_toggle():
    cfg = Config.load()
    assert "loudnorm=I=-14" in loudnorm_filter(cfg)
    cfg.override("render.loudnorm", False)
    assert loudnorm_filter(cfg) is None


def _words(spans):
    return [Word(s, e, f"w{i}") for i, (s, e) in enumerate(spans)]


def test_keep_ranges_collapses_long_silence():
    # Two speech runs with a 5s dead gap between them.
    words = _words([(0.0, 1.0), (1.2, 2.0), (7.0, 8.0)])
    ranges = plan_keep_ranges(words, 0.0, 8.0, min_silence=0.8, max_gap=0.3, pad=0.1)
    # The 5s gap must be trimmed away -> total kept far less than full 8s.
    assert total_kept(ranges) < 5.0
    assert len(ranges) == 2


def test_keep_ranges_no_words_keeps_all():
    ranges = plan_keep_ranges([], 2.0, 12.0)
    assert ranges == [(2.0, 12.0)]


def test_remap_time_is_monotonic_and_compresses():
    ranges = [(0.0, 2.0), (7.0, 9.0)]
    assert remap_time(0.0, ranges) == 0.0
    assert remap_time(2.0, ranges) == 2.0
    # A point inside the dropped gap maps to the end of the previous kept range.
    assert remap_time(5.0, ranges) == 2.0
    # After the gap, the second range starts right after the first on the timeline.
    assert abs(remap_time(8.0, ranges) - 3.0) < 1e-6


def test_remap_words_drops_dead_air_words():
    ranges = [(0.0, 2.0), (7.0, 9.0)]
    words = _words([(0.5, 1.0), (4.0, 4.5), (7.5, 8.0)])  # middle word is in the gap
    out = remap_words(words, ranges)
    assert len(out) == 2  # the dead-air word is dropped
    assert out[0].end <= out[1].start  # still ordered on the compressed timeline


def test_select_expr_shape():
    expr = select_expr([(0.0, 2.0), (7.0, 9.0)], clip_start=0.0)
    assert "between(t,0.000,2.000)" in expr
    assert "+between(t,7.000,9.000)" in expr
