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
