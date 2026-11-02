"""STEP 5 — parallel clip render: worker/thread planning, thread-safe timing,
and a real 2-clip concurrent render."""

import os
import shutil
import subprocess
import threading

import pytest

from shortforge import timing
from shortforge.config import Config
from shortforge.models import Clip
from shortforge.reframe import build_filtergraph
from shortforge.render.render import plan_render, _threads_args, render_clip

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def test_plan_render_defaults_and_overrides():
    cfg = Config.load()
    w, t = plan_render(cfg, 2)
    assert 1 <= w <= 2                       # never more workers than clips
    assert t >= 0
    cfg.override("render.workers", 1)
    assert plan_render(cfg, 4) == (1, 0)     # explicit sequential -> no thread split
    cfg2 = Config.load()
    cfg2.override("render.workers", 3)
    w2, t2 = plan_render(cfg2, 5)
    assert w2 == 3 and t2 >= 1                # threads split across workers
    assert plan_render(Config.load(), 1)[0] == 1   # one clip -> one worker


def test_threads_args():
    cfg = Config.load()
    assert _threads_args(cfg) == []          # 0 => auto (ffmpeg decides)
    cfg.override("render.threads", 4)
    assert _threads_args(cfg) == ["-threads", "4"]


def test_timings_threadsafe_under_concurrent_records():
    t = timing.Timings()
    tok = timing.activate(t)
    try:
        def worker(i):
            timing.activate(t)               # per-thread active Timings
            for _ in range(50):
                t.record_ffmpeg(["ffmpeg", f"-i{i}"], 0.01)
                t.add("render", 0.01)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    finally:
        timing.deactivate(tok)
    assert len(t.ffmpeg) == 8 * 50           # no lost/corrupted entries under contention
    render = [s for s in t.stages if s["stage"] == "render"][0]
    assert abs(render["seconds"] - 8 * 50 * 0.01) < 1.0   # accumulated safely


@pytest.fixture(scope="module")
def src_1080p(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("src") / "src.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-pix_fmt", "yuv420p", "-shortest", p],
        check=True, capture_output=True)
    return p


@needs_ffmpeg
def test_two_clips_render_concurrently(src_1080p, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    cfg = Config.load()
    cfg.override("render.threads", 2)                 # per-process cap (proves -threads plumbing)
    fg = build_filtergraph(1920, 1080, 1080, 1920, "crop")
    clips = [Clip(clip_id="01", source_hash="h", start=0.5, end=3.0, score=1.0),
             Clip(clip_id="02", source_hash="h", start=3.0, end=5.5, score=0.9)]
    outs = [str(tmp_path / f"c{c.clip_id}.mp4") for c in clips]

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(lambda co: render_clip(src_1080p, co[0], fg, cfg, co[1]),
                    zip(clips, outs)))

    for o in outs:                                    # both produced, both valid
        assert os.path.isfile(o) and os.path.getsize(o) > 0
