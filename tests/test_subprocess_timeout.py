"""A wedged child process must never hang the whole queue.

`utils.run()` — the single path every ffmpeg/ffprobe/espeak call goes through —
had no timeout. An ffmpeg that deadlocked (malformed input, stalled hardware
encoder) blocked its thread forever: the job never finished, the queue never
advanced, the dashboard sat on "Working…" and no notification ever fired. From
a phone that is indistinguishable from "still rendering", which is exactly the
state the operator cannot diagnose remotely.

Whisper and Demucs are in-process libraries and are deliberately NOT affected —
only real subprocesses are bounded here.
"""

import subprocess
import time

import pytest

from shortforge import utils
from shortforge.utils import ShortForgeError


def test_a_wedged_command_is_killed_and_reported_not_waited_on():
    t0 = time.time()
    with pytest.raises(ShortForgeError) as ei:
        utils.run(["sleep", "30"], timeout=1.0)
    assert time.time() - t0 < 10, "run() waited instead of enforcing the ceiling"
    assert "did not finish within" in str(ei.value)


def test_the_timeout_error_says_how_to_raise_the_ceiling():
    """A ceiling that fires on legitimate work must be self-service to fix, or
    it just trades one stuck state for another."""
    with pytest.raises(ShortForgeError) as ei:
        utils.run(["sleep", "30"], timeout=0.5)
    assert "SHORTFORGE_FFMPEG_TIMEOUT" in str(ei.value)


def test_normal_commands_are_unaffected():
    assert utils.run(["echo", "hello"]).stdout.strip() == "hello"


def test_ffprobe_is_bounded_far_tighter_than_ffmpeg():
    """A probe is a metadata read (seconds); a render is minutes. One ceiling
    for both would either be uselessly loose or dangerously tight."""
    probe = utils._timeout_for(["/usr/bin/ffprobe", "-i", "x.mp4"])
    render = utils._timeout_for(["/usr/bin/ffmpeg", "-i", "x.mp4"])
    other = utils._timeout_for(["espeak"])
    assert probe < other < render
    # ~70x this operator's observed ~100s per-clip render: catches hangs,
    # never a slow-but-real encode.
    assert render >= 3600


def test_a_timeout_is_a_normal_job_failure_so_the_queue_keeps_going(tmp_path, monkeypatch):
    """The point of the ceiling: one wedged link fails and the worker moves on,
    instead of the whole queue stopping dead."""
    from shortforge import queue as Q
    from shortforge import runner
    from shortforge.config import Config

    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_job(q, "https://a/wedges")
    Q.add_job(q, "https://a/is-fine")
    Q.save_queue(q, work)

    calls = {"n": 0}

    def fake_pipeline(url, cfg, **kw):
        calls["n"] += 1
        if "wedges" in url:
            raise ShortForgeError("ffmpeg did not finish within 7200s and was stopped")
        return {"clips": [{"file_path": "ok.mp4"}]}

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    monkeypatch.setattr("shortforge.selfheal.report", lambda *a, **k: (False, "no fix"))

    s = runner.drain_queue(lambda: Config.load(), work, announce=lambda m: None)

    assert calls["n"] == 2, "the worker stopped instead of continuing past the failure"
    assert s["stopped_because"] == "empty"
    counts = Q.counts(Q.load_queue(work))
    assert counts[Q.FAILED] == 1 and counts[Q.DONE] == 1


def test_timeout_expired_is_translated_not_leaked(monkeypatch):
    """Callers catch ShortForgeError; a raw TimeoutExpired would escape the
    queue's per-job error handling and take the whole drain down."""
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=1)

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(ShortForgeError):
        utils.run(["ffmpeg", "-i", "x"])
