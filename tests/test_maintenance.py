"""Disk housekeeping, and the data-loss bug hiding in the old cache clear.

`clear_cache(work_dir, "all")` deleted every entry in the work dir. But the
work dir holds the program's live state next to its caches — so
`python cli.py cache clear`, the command an operator reaches for exactly when
the disk is filling, destroyed the entire pending queue, the worker's run
state and the chat history, to reclaim space that was almost all in the
caches it was actually aiming at.
"""

import os
import time

from shortforge import lifecycle as L
from shortforge import maintenance as M
from shortforge import queue as Q
from shortforge import remote_control as RC
from shortforge.cache import clear_cache, protected_names


def _work(tmp_path):
    w = str(tmp_path / ".shortforge")
    os.makedirs(w, exist_ok=True)
    return w


# --- clearing caches must never take the queue with it ------------------------ #

def test_clear_all_keeps_the_queue_run_state_and_chat(tmp_path):
    work = _work(tmp_path)
    q = Q.load_queue(work)
    for i in range(10):
        Q.add_job(q, f"https://youtu.be/link-{i}", {"num_clips": 3})
    Q.save_queue(q, work)
    L.mark_online(work, "listen")
    RC.log_exchange(work, "ntfy", "hello", "hi")
    os.makedirs(os.path.join(work, "abc123hash"))        # a real cache dir
    os.makedirs(os.path.join(work, "downloads"))

    removed = clear_cache(work, "all")

    assert removed == 2                                   # only the two caches
    assert Q.counts(Q.load_queue(work))[Q.PENDING] == 10, "the queue was destroyed"
    assert L.read_state(work) is not None, "the worker's run state was destroyed"
    assert len(RC.read_chat_log(work)) == 1, "the chat history was destroyed"


def test_clear_all_still_removes_the_actual_caches(tmp_path):
    """The guard must not turn the command into a no-op."""
    work = _work(tmp_path)
    os.makedirs(os.path.join(work, "deadbeef"))
    open(os.path.join(work, "deadbeef", "transcript.json"), "w").write("{}")
    os.makedirs(os.path.join(work, "downloads"))

    assert clear_cache(work, "all") == 2
    assert not os.path.exists(os.path.join(work, "deadbeef"))
    assert not os.path.exists(os.path.join(work, "downloads"))


def test_the_protected_list_tracks_the_modules_that_own_each_name():
    """Imported from source rather than hardcoded, so renaming a state file
    can't silently drop it out of the protected set."""
    from shortforge.cache import is_protected
    from shortforge.runner import JOB_LOG_FILE
    names = protected_names()
    for owned in (Q.QUEUE_FILE, Q.LOCK_FILE, L.STATE_FILE, L.UI_STATE_FILE,
                  RC.CHAT_LOG_FILE, JOB_LOG_FILE):
        assert owned in names
        assert is_protected(owned)
        # Atomic writes land on a per-writer sibling "<file>.<random>.tmp"
        # (a shared "<file>.tmp" let concurrent writers corrupt each other),
        # so an in-flight temp must be matched by prefix, not exact name.
        assert is_protected(f"{owned}.a1b2c3.tmp")
    assert not is_protected("somecachedir")
    assert not is_protected("deadbeef.tmp")


def test_a_lock_held_by_a_live_worker_survives_a_cache_clear(tmp_path):
    """Deleting queue.lock under a running worker re-opens the double-render
    window the lock exists to close."""
    work = _work(tmp_path)
    assert Q.acquire_lock(work) is True
    try:
        clear_cache(work, "all")
        assert Q.acquire_lock(work) is False, "the lock was cleared out from under a worker"
    finally:
        Q.release_lock(work)


# --- proactive pruning -------------------------------------------------------- #

def _download(work, name, age_hours=0.0, size=1000):
    d = os.path.join(work, "downloads")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(p, (old, old))
    return p


def test_prune_removes_old_downloads_and_keeps_recent_ones(tmp_path):
    work = _work(tmp_path)
    _download(work, "ancient.mp4", age_hours=99, size=5000)
    _download(work, "fresh.mp4", age_hours=0, size=2000)

    n, freed = M.prune_downloads(work, keep_hours=48)

    assert (n, freed) == (1, 5000)
    assert os.listdir(os.path.join(work, "downloads")) == ["fresh.mp4"]


def test_prune_never_touches_a_download_in_flight(tmp_path):
    """Age is the whole safety mechanism: the file the running job is writing
    is minutes old, far inside any sane cutoff."""
    work = _work(tmp_path)
    _download(work, "being-written-right-now.mp4")
    n, _ = M.prune_downloads(work, keep_hours=1)
    assert n == 0


def test_dry_run_reports_without_deleting(tmp_path):
    work = _work(tmp_path)
    _download(work, "old.mp4", age_hours=99, size=7000)
    n, freed = M.prune_downloads(work, keep_hours=48, dry_run=True)
    assert (n, freed) == (1, 7000)
    assert os.path.exists(os.path.join(work, "downloads", "old.mp4"))


def test_prune_on_a_missing_downloads_dir_is_a_no_op(tmp_path):
    assert M.prune_downloads(_work(tmp_path)) == (0, 0)


def test_report_separates_reclaimable_bytes_from_paid_work(tmp_path):
    work = _work(tmp_path)
    _download(work, "a.mp4", size=4000)
    os.makedirs(os.path.join(work, "hookscores"))
    open(os.path.join(work, "hookscores", "deadbeef.json"), "w").write("[]")

    r = M.cache_report(work)

    assert r["downloads_bytes"] == 4000 and r["downloads_files"] == 1
    assert r["hookscores_files"] == 1
    # hook scores are paid LLM output — never counted as free space
    assert r["reclaimable_bytes"] == r["downloads_bytes"]


def test_the_emergency_disk_remedy_spares_the_paid_hook_score_cache(tmp_path, monkeypatch):
    """selfheal's "disk full" remedy used to delete hookscores too. Those are
    kilobytes of hash-named JSON, each one a paid LLM hook-scoring pass —
    clearing them buys no meaningful space and costs real money to rebuild."""
    from shortforge import selfheal
    from shortforge.config import Config

    work = _work(tmp_path)
    _download(work, "big.mp4", size=9000)
    os.makedirs(os.path.join(work, "hookscores"))
    open(os.path.join(work, "hookscores", "scores.json"), "w").write("[1,2,3]")

    monkeypatch.setattr(Config, "load", classmethod(
        lambda cls, path=None: Config({"paths": {"work_dir": work}})))

    ok, msg = selfheal._clear_cache()

    assert ok and "GB" in msg
    assert not os.path.exists(os.path.join(work, "downloads"))          # space freed
    assert os.path.exists(os.path.join(work, "hookscores", "scores.json")), \
        "the paid hook-score cache was destroyed to reclaim kilobytes"


# --- the per-video working folders (what actually fills the disk) ------------- #
# The operator found .shortforge at ~5 GB full of audio files. Pruning only
# covered downloads/ — the per-source-hash folders, which hold source_48k.wav
# (~330 MB for a 29-minute video) and audio16k.wav, were never touched.

def _source_workspace(work, name="a1b2c3d4", age_hours=0.0, size=1000):
    d = os.path.join(work, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "source_48k.wav"), "wb") as f:
        f.write(b"x" * size)
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(os.path.join(d, "source_48k.wav"), (old, old))
        os.utime(d, (old, old))
    return d


def test_source_workspaces_count_as_reclaimable(tmp_path):
    work = _work(tmp_path)
    _download(work, "v.mp4", size=2000)
    _source_workspace(work, size=8000)
    r = M.cache_report(work)
    assert r["source_cache_dirs"] == 1 and r["source_cache_bytes"] == 8000
    # the big WAV folders used to be invisible here, so "reclaimable" understated
    # the recoverable space by an order of magnitude
    assert r["reclaimable_bytes"] == 10000


def test_old_source_workspaces_are_pruned(tmp_path):
    work = _work(tmp_path)
    d = _source_workspace(work, "old00001", age_hours=99, size=5000)
    n, freed = M.prune_source_caches(work, keep_hours=48)
    assert (n, freed) == (1, 5000)
    assert not os.path.exists(d)


def test_a_workspace_the_running_job_is_using_is_never_pruned(tmp_path):
    """Judged by the newest file inside, so an actively-written folder is safe
    even if the directory entry itself looks older."""
    work = _work(tmp_path)
    d = _source_workspace(work, "busy0001", age_hours=99, size=1000)
    with open(os.path.join(d, "transcript.json"), "w") as f:   # just written
        f.write("{}")
    assert M.prune_source_caches(work, keep_hours=48) == (0, 0)
    assert os.path.exists(d)


def test_pruning_workspaces_never_touches_downloads_or_hook_scores(tmp_path):
    work = _work(tmp_path)
    _download(work, "keep.mp4", age_hours=99, size=4000)
    os.makedirs(os.path.join(work, "hookscores"))
    open(os.path.join(work, "hookscores", "s.json"), "w").write("[]")
    _source_workspace(work, "old00002", age_hours=99, size=6000)

    n, freed = M.prune_source_caches(work, keep_hours=48)

    assert (n, freed) == (1, 6000)                       # only the workspace
    assert os.path.exists(os.path.join(work, "downloads", "keep.mp4"))
    assert os.path.exists(os.path.join(work, "hookscores", "s.json"))


def test_human_gb_scales_units():
    assert M.human_gb(5_000) == "5 KB"
    assert M.human_gb(5 * 1024 ** 2) == "5 MB"
    assert M.human_gb(3 * 1024 ** 3) == "3.0 GB"
    assert M.human_gb(None) == "?"


# --- opt-in automatic clean-up ------------------------------------------------ #

def test_autoclean_is_off_unless_switched_on(tmp_path, monkeypatch):
    """Nothing deletes the operator's data on its own initiative."""
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "p.json"))
    work = _work(tmp_path)
    _download(work, "old.mp4", age_hours=99, size=5000)
    assert M.autoclean(work) == (0, 0)
    assert os.path.exists(os.path.join(work, "downloads", "old.mp4"))


def test_autoclean_when_enabled_prunes_downloads_and_workspaces(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "p.json"))
    from shortforge.providers import store as S
    S.save_store({"providers": [], "credentials": [],
                  "autoclean": {"enabled": True, "keep_hours": 48}})
    work = _work(tmp_path)
    _download(work, "old.mp4", age_hours=99, size=5000)
    _source_workspace(work, "oldws001", age_hours=99, size=7000)
    _download(work, "fresh.mp4", size=1000)

    n, freed = M.autoclean(work)

    assert (n, freed) == (2, 12000)
    assert os.listdir(os.path.join(work, "downloads")) == ["fresh.mp4"]
