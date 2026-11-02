"""Logo / watermark removal: found on synthetic videos with a known, faint mark."""

import os
import shutil
import subprocess

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
if not shutil.which("ffmpeg"):
    pytest.skip("ffmpeg not installed", allow_module_level=True)

from shortforge import delogo as DL  # noqa: E402
from shortforge.config import Config  # noqa: E402
from shortforge.utils import ShortForgeError  # noqa: E402

W, H, FPS, SECS = 320, 180, 20, 8
TEXT_BOX = (200, 145, 110, 25)            # x, y, w, h around the "BRAND" wordmark
DOT_BOX = (14, 14, 32, 32)                # around the faint dot


def _marks(text_scale=0.7):
    text = np.zeros((H, W), np.float32)
    cv2.putText(text, "BRAND", (205, 165), cv2.FONT_HERSHEY_SIMPLEX, text_scale, 1.0, 2,
                cv2.LINE_AA)
    dot = np.zeros((H, W), np.float32)
    cv2.circle(dot, (30, 30), 12, 1.0, -1, cv2.LINE_AA)
    return text, dot


def _video(path, *, alpha=0.06, still=False, edge_object=False, seed=1, text_scale=0.7):
    rng = np.random.default_rng(seed)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                          "-s", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0", "-c:v", "libx264",
                          "-crf", "18", "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)
    text, dot = _marks(text_scale)
    scene = None
    for i in range(SECS * FPS):
        if still:
            if scene is None:
                scene = cv2.resize(rng.uniform(40, 200, (5, 8, 3)).astype(np.float32), (W, H),
                                   interpolation=cv2.INTER_CUBIC)
            frame = scene.copy()
            cv2.circle(frame, (int(W / 2 + 30 * np.sin(i / 8)), H // 2), 30, (90, 120, 180), -1)
        else:
            if i % FPS == 0:
                scene = cv2.resize(rng.uniform(20, 235, (5, 8, 3)).astype(np.float32),
                                   (W + 60, H), interpolation=cv2.INTER_CUBIC)
            frame = scene[:, (i % FPS) * 2:(i % FPS) * 2 + W].copy()
            cv2.circle(frame, ((i * 9) % W, int(H / 2 + 40 * np.sin(i / 6))), 20,
                       (250, 250, 250), -1)
        if edge_object:
            cv2.rectangle(frame, (0, 0), (30, 70), (30, 30, 30), -1)
        for m in (text, dot):
            frame = frame * (1 - alpha * m[..., None]) + 255 * alpha * m[..., None]
        p.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
    p.stdin.close()
    assert p.wait() == 0
    return str(path)


@pytest.fixture
def cfg(tmp_path):
    c = Config.load()
    c.override("paths.work_dir", str(tmp_path / "work"))
    c.override("delogo.samples", 32)
    return c


def _overlaps(region, box) -> bool:
    x, y, w, h = box
    return not (region["x"] + region["w"] < x or x + w < region["x"]
                or region["y"] + region["h"] < y or y + h < region["y"])


def test_finds_a_faint_wordmark_and_a_faint_dot(tmp_path, cfg):
    det = DL.detect(_video(tmp_path / "wm.mp4", alpha=0.06), cfg)
    assert det.found and len(det.regions) == 2
    assert any(_overlaps(r, TEXT_BOX) for r in det.regions)
    assert any(_overlaps(r, DOT_BOX) for r in det.regions)
    assert det.describe() == "logo removed (2 areas)"
    assert os.path.isfile(det.mask_path)


def test_a_clean_video_has_nothing_removed(tmp_path, cfg):
    det = DL.detect(_video(tmp_path / "clean.mp4", alpha=0.0, seed=3), cfg)
    assert not det.found and det.describe() == "logo removal: none found"


def test_still_camera_keeps_scenery_touching_the_edge(tmp_path, cfg):
    """With nothing moving behind it, a fixed thing is only taken for a logo when
    it is corner-sized and clear of the frame edge — a wall or shelf running
    off-screen is scenery."""
    det = DL.detect(_video(tmp_path / "still.mp4", alpha=0.25, still=True, edge_object=True,
                           text_scale=0.45), cfg)
    assert det.found
    assert any(_overlaps(r, TEXT_BOX) for r in det.regions)
    assert not any(_overlaps(r, (0, 0, 12, 70)) for r in det.regions)


def test_scan_is_cached(tmp_path, cfg, monkeypatch):
    src = _video(tmp_path / "wm.mp4")
    first = DL.detect(src, cfg)
    monkeypatch.setattr(DL, "_sample_frames", lambda *a: pytest.fail("rescanned"))
    assert DL.detect(src, cfg).regions == first.regions


def test_cleaned_file_no_longer_shows_the_mark(tmp_path, cfg):
    marked = _video(tmp_path / "wm.mp4", alpha=0.3)
    truth = _video(tmp_path / "truth.mp4", alpha=0.0)
    det = DL.detect(marked, cfg)
    out = str(tmp_path / "out.mp4")
    DL.clean_file(marked, out, det)

    def frames(p):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-f", "rawvideo",
                              "-pix_fmt", "gray", "-"], capture_output=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.float32)

    t, before, after = frames(truth), frames(marked), frames(out)
    x, y, w, h = DOT_BOX
    err_before = np.abs(before[:, y:y + h, x:x + w] - t[:, y:y + h, x:x + w]).mean()
    err_after = np.abs(after[:, y:y + h, x:x + w] - t[:, y:y + h, x:x + w]).mean()
    assert err_after < err_before / 3


def test_filter_keeps_boxes_inside_the_frame():
    det = DL.Detection(width=320, height=180, mask_path="m.png", regions=[
        {"x": 0, "y": 0, "w": 40, "h": 30}, {"x": 300, "y": 170, "w": 40, "h": 30}])
    assert DL.ffmpeg_filter(det) == "delogo=x=1:y=1:w=40:h=30,delogo=x=300:y=170:w=19:h=9"
    assert DL.ffmpeg_filter(DL.Detection(width=320, height=180)) is None


def test_unreadable_video_fails_loudly(tmp_path, cfg):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(ShortForgeError):
        DL.detect(str(bad), cfg)


def test_removal_runs_before_the_crop():
    from shortforge.render.compose import build_filtergraph
    fg = build_filtergraph(1920, 1080, 1080, 1920, remove_logo="delogo=x=1:y=1:w=9:h=9",
                           video_select="between(t,0,5)")
    assert fg.index("delogo") < fg.index("select") < fg.index("crop=")
    assert "delogo" not in build_filtergraph(1920, 1080, 1080, 1920)


def test_a_short_with_a_logo_is_saved_without_it(tmp_path, cfg):
    from shortforge.shorts import youtube as YT
    staging = tmp_path / "staging"
    staging.mkdir()
    src = _video(staging / "abc.mp4", alpha=0.3)
    path, note = YT._remove_logo(src, str(staging), cfg, "abc")
    assert note == "logo removed (2 areas)" and path != src and os.path.isfile(path)


def test_a_short_whose_scan_fails_fails_instead_of_keeping_the_logo(tmp_path, cfg):
    from shortforge.shorts import youtube as YT
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "abc.mp4").write_bytes(b"broken")
    with pytest.raises(ShortForgeError, match="logo removal failed"):
        YT._remove_logo(str(staging / "abc.mp4"), str(staging), cfg, "abc")
    assert not staging.exists()


def test_queue_setting_switches_it_on_for_that_link_only():
    from shortforge import queue as Q
    cfg = Config.load()
    Q.apply_job_settings(cfg, {"settings": {"remove_logos": True}})
    assert DL.enabled(cfg) is True
    assert DL.enabled(Config.load()) is False
