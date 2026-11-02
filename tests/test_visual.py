"""M3++ visual hook signal scoring (pure logic, no video decode)."""

from shortforge.config import Config
from shortforge.detect.visual import VisualSignals


def _vs(motion, face, cuts):
    n = len(motion)
    times = [i * 0.5 for i in range(n)]
    return VisualSignals(times=times, motion=motion, face_area=face, cut_times=cuts)


def test_motion_ranks_above_static():
    cfg = Config.load()
    hi, _ = _vs([0.2] * 4, [0] * 4, [0.5]).score_window(0, 2, cfg)
    lo, _ = _vs([0.0] * 4, [0] * 4, []).score_window(0, 2, cfg)
    assert hi > lo
    assert lo == 0.0


def test_face_presence_boosts():
    cfg = Config.load()
    with_face, _ = _vs([0.0] * 4, [0.2] * 4, []).score_window(0, 2, cfg)
    no_face, _ = _vs([0.0] * 4, [0.0] * 4, []).score_window(0, 2, cfg)
    assert with_face > no_face


def test_cuts_boost_score():
    cfg = Config.load()
    many, _ = _vs([0.05] * 4, [0] * 4, [0.2, 0.7, 1.2]).score_window(0, 2, cfg)
    none, _ = _vs([0.05] * 4, [0] * 4, []).score_window(0, 2, cfg)
    assert many > none


def test_empty_window_is_zero():
    cfg = Config.load()
    score, sig = _vs([0.5] * 4, [0.5] * 4, [0.5]).score_window(100, 110, cfg)
    assert score == 0.0 and sig == {}


def test_score_bounded():
    cfg = Config.load()
    score, sig = _vs([1.0] * 4, [1.0] * 4, [0.1, 0.5, 0.9, 1.3, 1.7]).score_window(0, 2, cfg)
    assert 0.0 <= score <= 1.0
    assert set(sig) == {"motion", "cut_density", "face"}
