"""Shared queue worker — used by both `cli.py queue run` and the Telegram bot.

One implementation so the two entry points can never drift in how they resume,
tag outputs, apply per-link settings or report progress.
"""

from __future__ import annotations

import time
from typing import Callable

from . import notify as N
from . import queue as Q
from .config import Config
from .pipeline import run_pipeline
from .utils import log


def fmt_hms(seconds: float) -> str:
    m, s = divmod(int(max(0, seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def run_one(job: dict, idx: int, total: int, make_cfg: Callable[[], Config],
            work_dir: str) -> tuple[bool, int, str]:
    """Run a single queued link. Returns (ok, n_clips, message).

    Never raises for pipeline failures — a bad link is recorded and the caller
    moves on to the next one.
    """
    cfg = make_cfg()
    Q.apply_job_settings(cfg, job)                       # this link's own settings
    cfg.override("paths.output_prefix", Q.job_slug(job, idx))
    cfg.override("render.resume", True)                  # continue a part-done job
    t0 = time.time()
    try:
        manifest = run_pipeline(job["url"], cfg, owner_confirmed=True,
                                transcript_path=None, confirm_cost=lambda est: True)
    except Exception as e:  # noqa: BLE001
        q = Q.load_queue(work_dir)
        Q.mark(q, job["id"], Q.FAILED, error=str(e)[:500])
        Q.save_queue(q, work_dir)
        log.error("queue: job %s FAILED: %s", job["id"], e)
        return False, 0, f"⚠️ <b>Link {idx}/{total} failed</b>\n{job['url'][:80]}\n{str(e)[:300]}"

    clips = [c.get("file_path") for c in manifest.get("clips", [])]
    q = Q.load_queue(work_dir)
    Q.mark(q, job["id"], Q.DONE, clips=clips)
    Q.save_queue(q, work_dir)
    took = fmt_hms(time.time() - t0)
    left = Q.counts(q)[Q.PENDING]
    title = (manifest.get("source") or {}).get("title", job["url"])[:80]
    log.info("queue: job %s done — %d clip(s) in %s", job["id"], len(clips), took)
    return True, len(clips), (
        f"✅ <b>Link {idx}/{total} done</b> — {len(clips)} clip(s) in {took}\n{title}\n"
        + (f"➡️ Moving to the next link ({left} left)." if left else "That was the last one."))


def drain_queue(make_cfg: Callable[[], Config], work_dir: str,
                should_stop: Callable[[], bool] | None = None,
                announce: Callable[[str], None] | None = None) -> dict:
    """Work through every pending link, one at a time. Returns a summary dict.

    Re-reads the queue each iteration so links added meanwhile (UI, Telegram) are
    picked up without a restart.
    """
    announce = announce or N.notify
    t_all = time.time()
    made = 0
    while True:
        if should_stop is not None and should_stop():
            break
        q = Q.load_queue(work_dir)
        job = Q.next_pending(q)
        if job is None:
            break
        idx = q["jobs"].index(job) + 1
        total = len(q["jobs"])
        Q.mark(q, job["id"], Q.RUNNING)
        Q.save_queue(q, work_dir)
        log.info("=== queue %d/%d [%s] %s ===", idx, total, job["id"], job["url"])
        ok, n, msg = run_one(job, idx, total, make_cfg, work_dir)
        made += n
        announce(msg)

    q = Q.load_queue(work_dir)
    c = Q.counts(q)
    return {"made": made, "done": c[Q.DONE], "failed": c[Q.FAILED],
            "total_clips": Q.total_clips(q), "elapsed": time.time() - t_all}
