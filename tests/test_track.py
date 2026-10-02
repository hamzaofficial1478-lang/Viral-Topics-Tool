"""M5 subject-tracking path math (no video decode needed)."""

from shortforge.reframe.track import Track, _fill_gaps, _smooth


def _track():
    # 1920x1080 source -> 9:16 crop is 608x1080 (compute_crop-style dims).
    return Track(
        cw=608, ch=1080, times=[0.0, 1.0, 2.0],
        cx=[500.0, 900.0, 1300.0], cy=[540.0, 540.0, 540.0],
        src_w=1920, src_h=1080,
    )


def test_center_interpolates():
    tr = _track()
    assert tr.center_at(0.0)[0] == 500.0
    assert tr.center_at(1.0)[0] == 900.0
    assert abs(tr.center_at(0.5)[0] - 700.0) < 1e-6   # linear midpoint
    # Clamps to the ends outside the sampled range.
    assert tr.center_at(-5.0)[0] == 500.0
    assert tr.center_at(99.0)[0] == 1300.0


def test_topleft_is_clamped_in_bounds():
    tr = _track()
    x, y = tr.topleft_at(2.0)  # center 1300 - 540 = 760, but max x = 1920-1080=840
    assert 0 <= x <= tr.src_w - tr.cw
    assert 0 <= y <= tr.src_h - tr.ch


def test_smooth_reduces_jitter():
    noisy = [0.0, 100.0, 0.0, 100.0, 0.0]
    sm = _smooth(noisy, window=3)
    assert len(sm) == len(noisy)
    # Middle value pulled toward the local mean, not left at an extreme.
    assert 20.0 < sm[2] < 80.0


def test_fill_gaps_interpolates_missing():
    samples = [(0.0, 0.0), None, (2.0, 0.0), None, None]
    filled = _fill_gaps(samples)
    assert filled is not None
    assert filled[1][0] == 1.0          # interpolated between 0 and 2
    assert filled[3] == filled[2]        # trailing gap holds the last value


def test_fill_gaps_all_missing_returns_none():
    assert _fill_gaps([None, None, None]) is None
