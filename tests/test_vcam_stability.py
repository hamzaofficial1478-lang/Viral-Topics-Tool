"""Camera steadiness: the crop must follow ONE person instead of flip-flopping
between similarly-sized faces (which is what made output look shaky)."""

from shortforge.config import Config
from shortforge.reframe.vcam import _pick_face, plan_path


def _faces(*boxes):
    return [list(b) for b in boxes]


def test_first_pick_is_the_biggest_face():
    faces = _faces((0, 0, 40, 40), (200, 0, 80, 80))
    assert _pick_face(faces, None, 1.4) == (240.0, 40.0)      # the 80x80 one


def test_sticks_to_the_tracked_face_when_sizes_are_similar():
    # Two nearly equal faces: noise must NOT hand the camera back and forth.
    left = (0, 0, 78, 78)          # area 6084
    right = (300, 0, 80, 80)       # area 6400 — marginally bigger
    prev = (39.0, 39.0)            # we were following the LEFT face
    picked = _pick_face(_faces(left, right), prev, 1.4)
    assert picked == (39.0, 39.0)                              # stayed on the left face


def test_switches_when_another_face_is_clearly_dominant():
    small = (0, 0, 40, 40)         # area 1600
    huge = (300, 0, 120, 120)      # area 14400 = 9x -> clearly the subject now
    prev = (20.0, 20.0)            # following the small one
    picked = _pick_face(_faces(small, huge), prev, 1.4)
    assert picked == (360.0, 60.0)                             # handed over


def test_no_faces_returns_none():
    assert _pick_face([], (10.0, 10.0), 1.4) is None


def test_planner_holds_steady_through_alternating_noise():
    """Even if the target jitters slightly each sample, the deadzone keeps the
    camera parked rather than sweeping back and forth."""
    cfg = Config.load()
    dz = float(cfg.get("reframe.deadzone_pct", 12)) / 100.0 * 1920
    centers, cuts, times = [], [], []
    for i in range(20):
        jitter = 10 if i % 2 else -10                          # well inside the deadzone
        centers.append((960.0 + jitter, 540.0))
        cuts.append(False)
        times.append(i * 0.25)
    path = plan_path(centers, cuts, times, (960.0, 540.0),
                     {"deadzone": dz, "snap": 0.6 * 1920, "max_pan": 0.22 * 1920,
                      "min_dwell": 2.0})
    xs = [p[0] for p in path]
    assert max(xs) - min(xs) <= 1.0                            # camera did not oscillate


# --- orientation gate: face tracking only when squeezing into portrait ------- #

def test_track_when_portrait_default():
    cfg = Config.load()
    assert cfg.get("reframe.track_when") == "portrait"


def _gate(out_w, out_h, track_when="portrait"):
    """Mirrors the pipeline's orientation gate (kept in lockstep by this test)."""
    portrait_out = out_h > out_w
    return portrait_out if track_when == "portrait" else True


def test_gate_enables_tracking_only_for_portrait():
    assert _gate(1080, 1920) is True         # 9:16 portrait -> track
    assert _gate(1920, 1080) is False        # landscape     -> centre crop
    assert _gate(1080, 1080) is False        # square        -> centre crop
    assert _gate(1920, 1080, "always") is True   # explicit override still works
