"""Persistent link queue: per-link settings, ordering, crash resume, output tags."""

import json
import os
import time

import pytest

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


# --- single-worker lock ------------------------------------------------------- #

def test_lock_is_exclusive(tmp_path):
    work = str(tmp_path)
    assert Q.acquire_lock(work) is True
    assert os.path.isfile(Q.lock_path(work))
    assert Q.acquire_lock(work) is False       # a second holder is refused
    Q.release_lock(work)
    assert Q.acquire_lock(work) is True        # free again after release


def test_stale_lock_from_a_dead_pid_is_reclaimed(tmp_path):
    work = str(tmp_path)
    os.makedirs(work, exist_ok=True)
    with open(Q.lock_path(work), "w", encoding="utf-8") as f:
        f.write("999999999")                   # a PID that (almost certainly) isn't running
    assert Q.acquire_lock(work) is True         # reclaimed, not refused forever
    with open(Q.lock_path(work), encoding="utf-8") as f:
        assert f.read().strip() == str(os.getpid())


def test_lock_held_by_a_live_pid_is_not_stolen(tmp_path):
    work = str(tmp_path)
    os.makedirs(work, exist_ok=True)
    with open(Q.lock_path(work), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))               # this process is definitely alive
    assert Q.acquire_lock(work) is False


def test_drain_queue_refuses_to_double_run_while_locked(tmp_path):
    """The bug this guards: two drain_queue() runs picking up and rendering the
    same job. If the lock is already held, a second drain must skip cleanly
    rather than race the first for the pending job."""
    from shortforge import runner

    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_job(q, "https://a/1")
    Q.save_queue(q, work)

    assert Q.acquire_lock(work) is True         # simulate another worker running
    try:
        calls = []
        s = runner.drain_queue(lambda: Config.load(), work, announce=calls.append)
    finally:
        Q.release_lock(work)
    assert s["made"] == 0
    assert calls == []                          # never even started the job
    assert Q.counts(Q.load_queue(work))[Q.PENDING] == 1   # still there for the real worker
    assert s["stopped_because"] == "locked"


# --- a drain that did nothing must SAY it did nothing ------------------------- #
# The operator pressed "Start working" and got three phone messages at once:
# "ShortForge started", "All links processed", "ShortForge stopped" — while the
# link they had added was still sitting pending, untouched. The drain had
# exited instantly (the queue was paused) but returned a dict indistinguishable
# from a completed run, so the caller announced success over work never done.

# --- adding links WHILE a job runs -------------------------------------------- #
# `cli.py listen` polls ntfy on one thread and drains the queue on another, both
# writing queue.json. Before the mutex + per-writer temp files, an add landing at
# the same moment as a status update could: crash the poller (both writers shared
# one "queue.json.tmp", so one renamed the other's out from under it), CORRUPT the
# file into unparseable JSON — after which load_queue "starts empty" and the whole
# queue is gone — or silently lose either the completion or the new link.

def test_adding_links_while_the_worker_writes_never_loses_or_corrupts(tmp_path):
    import threading

    work = str(tmp_path)
    q = Q.load_queue(work)
    job = Q.add_job(q, "https://youtu.be/RUNNINGJOB")
    Q.mark(q, job["id"], Q.RUNNING)
    Q.save_queue(q, work)
    jid = job["id"]

    problems = []
    for trial in range(40):
        Q.mutate_queue(work, lambda x: (
            x.update(jobs=[j for j in x["jobs"] if j["id"] == jid]),
            Q.mark(x, jid, Q.RUNNING), x["jobs"][0].update(clips=[])))

        def worker():
            try:
                Q.mutate_queue(work, lambda w: (
                    time.sleep(0.0005),
                    Q.mark(w, jid, Q.DONE, clips=["a.mp4", "b.mp4", "c.mp4"]))[1])
            except Exception as e:                      # noqa: BLE001
                problems.append(f"worker crashed: {e}")

        def adder(n=trial):
            try:
                Q.mutate_queue(work, lambda a: (
                    time.sleep(0.0005),
                    Q.add_links(a, [f"https://youtu.be/NEW{n:04d}"]))[1])
            except Exception as e:                      # noqa: BLE001
                problems.append(f"add crashed: {e}")

        t1, t2 = threading.Thread(target=worker), threading.Thread(target=adder)
        t1.start(); t2.start(); t1.join(); t2.join()

        final = Q.load_queue(work)
        if not final["jobs"]:
            problems.append("queue file was corrupted and read back empty")
            break
        done = next((j for j in final["jobs"] if j["id"] == jid), None)
        if done is None or done["status"] != Q.DONE or len(done["clips"]) != 3:
            problems.append("the finished job was rolled back / its clips erased")
        if not any(f"NEW{trial:04d}" in j["url"] for j in final["jobs"]):
            problems.append("the newly added link was lost")

    assert not problems, problems[:3]


def test_two_writers_never_share_a_scratch_file(tmp_path):
    """The corruption's root cause: every atomic writer built its temp path as
    "<file>.tmp", one name shared by all of them."""
    import threading

    work = str(tmp_path)
    Q.save_queue({"jobs": []}, work)
    seen = []
    real_mkstemp = __import__("tempfile").mkstemp

    def spy(*a, **k):
        fd, path = real_mkstemp(*a, **k)
        seen.append(path)
        return fd, path

    import tempfile as _t
    _t.mkstemp = spy
    try:
        ts = [threading.Thread(target=Q.save_queue, args=({"jobs": []}, work))
              for _ in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        _t.mkstemp = real_mkstemp
    assert len(seen) == len(set(seen)), "two writers shared a temp filename"


def test_a_link_added_mid_drain_is_picked_up_without_another_start(tmp_path):
    """What the operator actually wants: send more links while one renders and
    have them run after it, with no second command."""
    from shortforge import runner

    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_links(q, ["https://youtu.be/FIRSTLINK1"])
    Q.save_queue(q, work)

    order = []

    def fake(url, cfg, **kw):
        order.append(url)
        if len(order) == 1:                  # arrives while the first is rendering
            Q.mutate_queue(work, lambda x: Q.add_links(x, ["https://youtu.be/SECONDLINK"]))
        return {"clips": [{"file_path": "a.mp4"}]}

    runner.run_pipeline = fake
    s = runner.drain_queue(lambda: Config.load(), work, announce=lambda m: None)

    assert order == ["https://youtu.be/FIRSTLINK1", "https://youtu.be/SECONDLINK"]
    assert s["stopped_because"] == "empty"
    assert Q.counts(Q.load_queue(work))[Q.DONE] == 2


# --- duplicate links, and the order links come out in ------------------------- #
# Adding the same video twice renders it twice: hours of CPU on this machine and
# paid spend a second time, for a byte-identical result. Nothing checked.

@pytest.mark.parametrize("a,b", [
    # the same video, every way a phone can hand it over
    ("https://youtu.be/dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ?si=AbCdEf"),
    ("https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "https://m.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/shorts/dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ/"),
    ("https://example.com/vid?a=1", "https://example.com/vid?a=1&utm_source=x"),
])
def test_the_same_video_shared_different_ways_is_one_link(a, b):
    """Sharing from the YouTube app appends a `?si=` tracking parameter, so a
    plain string comparison sees a new link every single time."""
    assert Q.canonical_url(a) == Q.canonical_url(b)


@pytest.mark.parametrize("a,b", [
    ("https://youtu.be/AAAAAAAAAAA", "https://youtu.be/BBBBBBBBBBB"),
    ("https://example.com/one", "https://example.com/two"),
    ("https://example.com/v?id=1", "https://example.com/v?id=2"),
])
def test_genuinely_different_links_stay_different(a, b):
    assert Q.canonical_url(a) != Q.canonical_url(b)


def test_adding_a_link_already_in_the_queue_is_refused(tmp_path):
    work = str(tmp_path)
    q = Q.load_queue(work)
    added, skipped = Q.add_links(q, ["https://youtu.be/dQw4w9WgXcQ"])
    assert len(added) == 1 and not skipped

    added2, skipped2 = Q.add_links(q, ["https://youtu.be/dQw4w9WgXcQ?si=xyz"])
    assert added2 == [] and len(skipped2) == 1
    assert len(q["jobs"]) == 1
    assert "already waiting" in Q.describe_skipped(skipped2)


def test_a_link_already_running_or_done_is_also_refused(tmp_path):
    """The operator's exact case: add one link, start it, then add five more —
    all six must be different, including against the one already working."""
    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_links(q, ["https://youtu.be/RUNNINGNOW"])
    Q.mark(q, q["jobs"][0]["id"], Q.RUNNING)

    added, skipped = Q.add_links(q, ["https://youtu.be/RUNNINGNOW",
                                     "https://youtu.be/BRANDNEW01"])
    assert [j["url"] for j in added] == ["https://youtu.be/BRANDNEW01"]
    assert len(skipped) == 1 and "running right now" in Q.describe_skipped(skipped)


def test_duplicates_inside_one_batch_are_caught_too(tmp_path):
    q = Q.load_queue(str(tmp_path))
    added, skipped = Q.add_links(q, ["https://youtu.be/SAMEVIDEO01",
                                     "https://youtu.be/SAMEVIDEO01?si=a",
                                     "https://youtu.be/OTHERVIDEO"])
    assert len(added) == 2 and len(skipped) == 1


def test_links_are_worked_in_the_order_they_were_added(tmp_path):
    """Never random: append order in, append order out, regardless of how many
    batches they arrived in or what settings each carried."""
    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_links(q, ["https://youtu.be/first-000"], {"num_clips": 5})
    Q.add_links(q, [f"https://youtu.be/batch-{i:03d}" for i in range(5)])
    Q.save_queue(q, work)

    order = []
    while True:
        q = Q.load_queue(work)
        job = Q.next_pending(q)
        if job is None:
            break
        order.append(job["url"])
        Q.mark(q, job["id"], Q.DONE)
        Q.save_queue(q, work)

    assert order == ["https://youtu.be/first-000"] + \
        [f"https://youtu.be/batch-{i:03d}" for i in range(5)]


def test_adding_while_one_is_running_does_not_disturb_the_pending_order(tmp_path):
    q = Q.load_queue(str(tmp_path))
    Q.add_links(q, ["https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb"])
    Q.mark(q, q["jobs"][0]["id"], Q.RUNNING)          # first one is working
    Q.add_links(q, ["https://youtu.be/ccccccccccc"])   # more arrive meanwhile
    assert Q.next_pending(q)["url"] == "https://youtu.be/bbbbbbbbbbb"


def test_a_blank_or_broken_link_never_collides_with_everything(tmp_path):
    """canonical_url must not map junk to a single shared identity, or one bad
    entry would silently block every later add."""
    q = Q.load_queue(str(tmp_path))
    assert Q.find_duplicate(q, "") is None
    added, _ = Q.add_links(q, ["not a url at all", "also not a url"])
    assert len(added) == 2


# --- a leftover lock must never wedge the queue forever ----------------------- #
# The operator's machine reached exactly this state: links added fine, ntfy
# replied, but nothing ever ran. A worker had been killed without releasing the
# lock, and PID liveness was the ONLY reclaim test — so once Windows recycled
# that PID onto an unrelated live process, `pid_alive()` answered True forever
# and every drain returned "locked". Nothing in the dashboard or ntfy could
# clear it.

def test_a_lock_whose_pid_was_reused_is_reclaimed_once_it_goes_cold(tmp_path):
    import os
    work = str(tmp_path)
    # A live PID (this interpreter) that is emphatically not a ShortForge worker.
    open(Q.lock_path(work), "w").write(str(os.getpid()))
    cold = time.time() - (Q.LOCK_STALE_AFTER + 60)
    os.utime(Q.lock_path(work), (cold, cold))

    assert Q.acquire_lock(work) is True, "a cold lock wedged the queue"
    Q.release_lock(work)


def test_a_lock_being_kept_warm_by_a_real_worker_is_still_respected(tmp_path):
    """The age test must not become a licence to steal a busy worker's lock —
    a single render legitimately runs longer than the staleness window."""
    work = str(tmp_path)
    assert Q.acquire_lock(work) is True
    try:
        Q.touch_lock(work)                       # what the heartbeat thread does
        assert Q.acquire_lock(work) is False
    finally:
        Q.release_lock(work)


def test_the_heartbeat_keeps_a_held_lock_warm(tmp_path):
    import os
    work = str(tmp_path)
    assert Q.acquire_lock(work) is True
    cold = time.time() - (Q.LOCK_STALE_AFTER + 60)
    os.utime(Q.lock_path(work), (cold, cold))    # pretend it aged while working

    stop = Q.start_lock_heartbeat(work, every=0.05)
    try:
        time.sleep(0.25)
        age = time.time() - os.path.getmtime(Q.lock_path(work))
        assert age < Q.LOCK_STALE_AFTER, "the heartbeat did not refresh the lock"
        assert Q.acquire_lock(work) is False     # so it is still protected
    finally:
        stop.set()
        Q.release_lock(work)


def test_a_dead_holder_is_still_reclaimed_immediately(tmp_path):
    """Unchanged behaviour: a dead PID never has to wait out the age window."""
    work = str(tmp_path)
    open(Q.lock_path(work), "w").write("999999")      # not a running process
    assert Q.acquire_lock(work) is True
    Q.release_lock(work)


def test_a_corrupt_lock_file_does_not_wedge_the_queue(tmp_path):
    """An unreadable lock used to mean 'refuse forever'; it now ages out."""
    import os
    work = str(tmp_path)
    open(Q.lock_path(work), "w").write("not-a-pid")
    cold = time.time() - (Q.LOCK_STALE_AFTER + 60)
    os.utime(Q.lock_path(work), (cold, cold))
    assert Q.acquire_lock(work) is True
    Q.release_lock(work)


def test_drain_recovers_on_its_own_from_a_wedged_lock(tmp_path):
    """End to end: the queue that could never start again now does."""
    import os
    from shortforge import runner

    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_job(q, "https://a/1")
    Q.save_queue(q, work)
    open(Q.lock_path(work), "w").write(str(os.getpid()))
    cold = time.time() - (Q.LOCK_STALE_AFTER + 60)
    os.utime(Q.lock_path(work), (cold, cold))

    s = runner.drain_queue(lambda: Config.load(), work, announce=lambda m: None)
    assert s["stopped_because"] != "locked"


def test_a_paused_drain_reports_why_it_did_nothing(tmp_path):
    from shortforge import runner

    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_job(q, "https://a/1")
    Q.set_paused(q, True)
    Q.save_queue(q, work)

    said = []
    s = runner.drain_queue(lambda: Config.load(), work, announce=said.append)

    assert s["stopped_because"] == "paused"
    assert s["made"] == 0 and s["pending"] == 1
    assert said == []                                     # nothing was started
    assert Q.counts(Q.load_queue(work))[Q.PENDING] == 1   # link untouched


def test_an_empty_queue_is_a_genuine_completion_not_a_pause(tmp_path):
    """The one case that legitimately earns "All links processed"."""
    from shortforge import runner

    work = str(tmp_path)
    Q.save_queue(Q.load_queue(work), work)
    s = runner.drain_queue(lambda: Config.load(), work, announce=lambda m: None)
    assert s["stopped_because"] == "empty" and s["pending"] == 0


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
