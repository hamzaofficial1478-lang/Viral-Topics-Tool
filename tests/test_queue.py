"""Persistent link queue: per-link settings, ordering, crash resume, output tags."""

import json
import os

from shortforge import queue as Q
from shortforge.config import Config


def test_add_keeps_order_and_own_settings():
    q = {"jobs": []}
    Q.add_job(q, "https://a", {"num_clips": 5, "duration": 120}, label="Pirates")
    Q.add_job(q, "https://b", {"num_clips": 6, "duration": 60})
    assert [j["url"] for j in q["jobs"]] == ["https://a", "https://b"]
    assert q["jobs"][0]["settings"] == {"num_clips": 5, "duration": 120}
    assert q["jobs"][1]["settings"] == {"num_clips": 6, "duration": 60}
    assert Q.next_pending(q)["url"] == "https://a"          # FIFO


def test_unknown_settings_are_dropped():
    q = {"jobs": []}
    j = Q.add_job(q, "https://a", {"num_clips": 3, "bogus": 1, "duration": None})
    assert j["settings"] == {"num_clips": 3}                # None and unknown keys ignored


def test_per_job_settings_applied_to_config():
    q = {"jobs": []}
    job = Q.add_job(q, "https://a", {"num_clips": 5, "duration": 120,
                                     "aspect": "1:1", "resolution": "720p"})
    cfg = Config.load()
    Q.apply_job_settings(cfg, job)
    assert cfg.get("select.num_clips") == 5
    assert cfg.get("select.target_duration") == 120
    assert cfg.get("reframe.aspect") == "1:1"
    assert cfg.get("reframe.resolution") == "720p"


def test_interrupted_job_returns_to_pending():
    """A power cut leaves a job 'running' — it must be picked up again, not lost."""
    q = {"jobs": []}
    job = Q.add_job(q, "https://a", {})
    Q.mark(q, job["id"], Q.RUNNING)
    assert Q.counts(q)[Q.RUNNING] == 1
    assert Q.requeue_interrupted(q) == 1
    assert Q.counts(q)[Q.PENDING] == 1 and Q.counts(q)[Q.RUNNING] == 0
    assert Q.next_pending(q)["id"] == job["id"]


def test_done_and_failed_jobs_are_not_rerun():
    q = {"jobs": []}
    a = Q.add_job(q, "https://a", {})
    b = Q.add_job(q, "https://b", {})
    c = Q.add_job(q, "https://c", {})
    Q.mark(q, a["id"], Q.DONE, clips=["out/a1.mp4", "out/a2.mp4"])
    Q.mark(q, b["id"], Q.FAILED, error="boom")
    assert Q.next_pending(q)["id"] == c["id"]               # skips done + failed
    assert Q.total_clips(q) == 2
    assert Q.requeue_interrupted(q) == 0                    # done/failed untouched


def test_persistence_round_trip_and_atomic_write(tmp_path):
    work = str(tmp_path)
    q = {"jobs": []}
    Q.add_job(q, "https://a", {"num_clips": 2}, label="My Show")
    Q.save_queue(q, work)
    assert os.path.isfile(Q.queue_path(work))
    assert not os.path.isfile(Q.queue_path(work) + ".tmp")   # temp file swapped away
    again = Q.load_queue(work)
    assert again["jobs"][0]["url"] == "https://a"
    assert again["jobs"][0]["settings"] == {"num_clips": 2}


def test_corrupt_queue_file_does_not_crash(tmp_path):
    work = str(tmp_path)
    with open(Q.queue_path(work), "w", encoding="utf-8") as f:
        f.write("{not json")
    assert Q.load_queue(work) == {"jobs": []}                # degrades, never raises


def test_job_slug_tags_outputs_readably():
    q = {"jobs": []}
    a = Q.add_job(q, "https://a", {}, label="Pirates & Guilds!")
    b = Q.add_job(q, "https://b", {})
    assert Q.job_slug(a, 1) == "pirates-guilds"              # safe for filenames
    assert Q.job_slug(b, 2) == "link02"                      # falls back to position
    assert len(Q.job_slug({"id": "x", "label": "y" * 99}, 1)) <= 32


# --- what the phone sees ----------------------------------------------------- #

def test_describe_settings_reads_like_a_sentence():
    job = {"settings": {"num_clips": 6, "duration": 90, "aspect": "16:9",
                        "resolution": "1080p"}}
    assert Q.describe_settings(job) == "6 clip(s) of 1m30s, 16:9, 1080p"
    assert Q.describe_settings({"settings": {}}) == "default settings"
    assert Q.describe_settings({"settings": {"num_clips": 3}}) == "3 clip(s)"
    assert Q.fmt_duration(60) == "1m00s" and Q.fmt_duration(45) == "45s"


def test_each_link_announces_its_start_with_the_numbers(tmp_path, monkeypatch):
    """After a reboot the operator wants to read WHICH link is running and how
    many clips it was told to make — not just 'online'."""
    from shortforge import runner

    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_job(q, "https://youtu.be/AAA", {"num_clips": 6, "duration": 60})
    Q.save_queue(q, work)

    said = []
    monkeypatch.setattr(runner, "run_one", lambda *a, **k: (True, 6, "done"))
    runner.drain_queue(lambda: Config.load(), work, announce=said.append)

    start = said[0]
    assert "Link 1/1 started" in start
    assert "6 clip(s) of 1m00s" in start and "https://youtu.be/AAA" in start


def test_the_running_link_is_recorded_for_crash_reporting(tmp_path, monkeypatch):
    """A power cut mid-render must leave enough on disk to name the link."""
    from shortforge import lifecycle, runner

    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_job(q, "https://youtu.be/BBB", {"num_clips": 2})
    Q.save_queue(q, work)

    seen = {}

    def _mid_job(*a, **k):
        seen["state"] = lifecycle.read_state(work)     # what a power cut would leave
        return True, 2, "done"

    monkeypatch.setattr(runner, "run_one", _mid_job)
    runner.drain_queue(lambda: Config.load(), work, announce=lambda m: None)

    assert seen["state"]["current"]["url"] == "https://youtu.be/BBB"
    assert "2 clip(s)" in seen["state"]["current"]["detail"]
    assert lifecycle.read_state(work)["current"] is None      # cleared when finished
