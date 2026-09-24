"""A4 virtual-camera planner: deadzone, snap, scene-cut reset, pan, clamp."""

from shortforge.reframe.vcam import plan_path, _clamp_center


# src_w = 1000 for easy percentages.
PARAMS = {"deadzone": 80.0, "snap": 450.0, "max_pan": 350.0, "min_dwell": 1.2}
FC = (500.0, 500.0)


def _times(n, dt=1.0):
    return [i * dt for i in range(n)]


def test_deadzone_ignores_small_movement():
    centers = [(500, 500), (540, 500)]  # 40px < 80px deadzone
    path = plan_path(centers, [False, False], _times(2), FC, PARAMS)
    assert path[1] == (500, 500)  # camera did NOT move


def test_hold_when_no_target():
    centers = [(500, 500), None, None]
    path = plan_path(centers, [False, False, False], _times(3), FC, PARAMS)
    assert path[1] == (500, 500) and path[2] == (500, 500)


def test_large_jump_snaps_after_dwell():
    # Hold on subject A for >min_dwell, then subject B appears far away.
    centers = [(500, 500), (500, 500), (980, 500)]  # 480px > 450 snap
    path = plan_path(centers, [False, False, False], _times(3), FC, PARAMS)
    assert path[2] == (980, 500)  # hard cut to B, not a slow sweep


def test_no_snap_before_min_dwell_pans_instead():
    # Big displacement but before dwell -> should pan (capped), not snap.
    centers = [(500, 500), (980, 500)]  # dwell at i=1 is 1.0 < 1.2
    path = plan_path(centers, [False, False], _times(2), FC, PARAMS)
    # panned by at most max_pan (350) from 500 -> ~850, not the full 980
    assert 500 < path[1][0] <= 500 + 350 + 1e-6
    assert path[1][0] < 980


def test_medium_move_is_capped_pan():
    centers = [(500, 500), (900, 500)]  # 400px: > deadzone, < snap
    path = plan_path(centers, [False, False], _times(2), FC, PARAMS)
    # step capped at 350; moves 350/400 of the way -> x=850
    assert abs(path[1][0] - 850.0) < 1e-6


def test_scene_cut_resets_to_new_target_regardless_of_deadzone():
    # Even a tiny post-cut displacement must re-target (state discarded).
    centers = [(500, 500), (520, 500)]
    path = plan_path(centers, [False, True], _times(2), FC, PARAMS)
    assert path[1] == (520, 500)  # snapped to the cut's detection


def test_scene_cut_recenters_when_no_detection():
    centers = [(500, 500), None]
    path = plan_path(centers, [False, True], _times(2), FC, PARAMS)
    assert path[1] == FC  # re-center on a cut with nothing detected


def test_clamp_keeps_window_in_bounds():
    # crop 400x720 in a 1000x720 frame; a center at x=50 would spill left.
    cx, cy = _clamp_center(50, 360, 400, 720, 1000, 720)
    assert cx == 200.0  # half of 400, so left edge sits at 0
    cx2, _ = _clamp_center(9999, 360, 400, 720, 1000, 720)
    assert cx2 == 800.0  # right edge sits at 1000


def test_empty_input():
    assert plan_path([], [], [], FC, PARAMS) == []
