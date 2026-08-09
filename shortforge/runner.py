"""Shared queue worker — used by both `cli.py queue run` and `cli.py listen`
(the ntfy phone-driven mode).

One implementation so the two entry points can never drift in how they resume,
tag outputs, apply per-link settings or report progress.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from . import lifecycle
from . import notify as N
from . import queue as Q
from .config import Config
from .pipeline import run_pipeline
from .utils import log

JOB_LOG_FILE = "current_job.log"

# The queue-driven path writes "LEVELNAME\tmessage" per line (see
# _attach_job_log) so a reader can tell warnings/errors apart from ordinary
# progress lines — the manual New Job form gets this for free from the log
# record's own levelno; the file has to carry it explicitly instead.
_LEVEL_NAMES = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
                "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}


def job_log_path(work_dir: str) -> str:
    return os.path.join(work_dir, JOB_LOG_FILE)


def _attach_job_log(work_dir: str) -> logging.Handler:
    """Start capturing this job's log lines to a file the dashboard can tail.

    The queue-driven path (`cli.py listen`'s worker thread, or a spawned
    `queue run` subprocess) runs in its own PROCESS, separate from the
    dashboard — unlike the manual "New job" form, which captures its own
    background THREAD's log lines into an in-memory queue within the same
    Streamlit process. There's no equivalent in-memory channel available
    across processes, so this uses a file instead: truncated fresh for each
    job (mode "w"), so a tail of it only ever shows THIS job's lines.
    """
    path = job_log_path(work_dir)
    os.makedirs(work_dir, exist_ok=True)
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(levelname)s\t%(message)s"))
    logging.getLogger("shortforge").addHandler(handler)
    return handler


def _detach_job_log(handler: logging.Handler) -> None:
    logging.getLogger("shortforge").removeHandler(handler)
    handler.close()


def read_job_log(work_dir: str) -> list[tuple[int, str]]:
    """The current job's log lines as (levelno, message) pairs, oldest first.

    A line without a recognised "LEVELNAME\\t" prefix (e.g. one written before
    this format existed, or by anything else that touches the file) is kept
    as-is at INFO — never dropped, never mis-attributed to a warning.
    """
    try:
        with open(job_log_path(work_dir), "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.read().splitlines()
    except OSError:
        return []
    out: list[tuple[int, str]] = []
    for ln in raw_lines:
        level_str, sep, msg = ln.partition("\t")
        levelno = _LEVEL_NAMES.get(level_str) if sep else None
        out.append((levelno, msg) if levelno is not None else (logging.INFO, ln))
    return out


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
        # Triage: apply a safe automatic remedy if one exists, then retry ONCE.
        from . import selfheal
        fixed, msg = selfheal.report(str(e), context=job["url"][:80])
        if fixed and int(job.get("attempts", 0)) < 2:
            log.info("self-heal succeeded — retrying job %s once", job["id"])
            try:
                manifest = run_pipeline(job["url"], cfg, owner_confirmed=True,
                                        transcript_path=None, confirm_cost=lambda est: True)
            except Exception as e2:  # noqa: BLE001 - retry failed; report the new error
                err2 = str(e2)[:500]   # bind now: Python unbinds `e2` at block exit
                Q.mutate_queue(work_dir,
                               lambda q: Q.mark(q, job["id"], Q.FAILED, error=err2))
                log.error("queue: job %s FAILED after self-heal: %s", job["id"], e2)
                return False, 0, (f"⚠️ <b>Link {idx}/{total} still failed after the fix</b>\n"
                                  f"{job['url'][:80]}\n{str(e2)[:300]}")
            else:
                clips = [c.get("file_path") for c in manifest.get("clips", [])]
                Q.mutate_queue(work_dir,
                               lambda q: Q.mark(q, job["id"], Q.DONE, clips=clips))
                return True, len(clips), (f"{msg}\n✅ <b>Link {idx}/{total} done after the fix</b> "
                                          f"— {len(clips)} clip(s)")
        err = str(e)[:500]             # bind now: Python unbinds `e` at block exit
        Q.mutate_queue(work_dir,
                       lambda q: Q.mark(q, job["id"], Q.FAILED, error=err))
        log.error("queue: job %s FAILED: %s", job["id"], e)
        return False, 0, f"{msg}\n<i>Link {idx}/{total}</i> — continuing with the rest."

    clips = [c.get("file_path") for c in manifest.get("clips", [])]
    q, _ = Q.mutate_queue(work_dir,
                          lambda qq: Q.mark(qq, job["id"], Q.DONE, clips=clips))
    took = fmt_hms(time.time() - t0)
    left = Q.counts(q)[Q.PENDING]
    title = (manifest.get("source") or {}).get("title", job["url"])[:80]
    log.info("queue: job %s done — %d clip(s) in %s", job["id"], len(clips), took)
    # If fewer clips came out than were asked for, say so here rather than
    # letting the operator count them and wonder. They asked for 6 and got 4
    # with no explanation anywhere.
    asked = int((job.get("settings") or {}).get("num_clips") or 0)
    short = (f"\n⚠️ You asked for {asked} — the source only had room for "
             f"{len(clips)} at this clip length." if asked and len(clips) < asked else "")
    return True, len(clips), (
        f"✅ <b>Link {idx}/{total} done</b> — {len(clips)} clip(s) in {took}\n{title}{short}\n"
        + (f"➡️ Moving to the next link ({left} left)." if left else "That was the last one."))


def drain_queue(make_cfg: Callable[[], Config], work_dir: str,
                should_stop: Callable[[], bool] | None = None,
                announce: Callable[[str], None] | None = None) -> dict:
    """Work through every pending link, one at a time. Returns a summary dict.

    Re-reads the queue each iteration so links added meanwhile (UI, ntfy) are
    picked up without a restart. Holds a whole-run lock (``queue.lock``) so a
    second drain_queue() — a hand-run `queue run` while `listen`'s worker is
    also draining, say — can't pick up and render the same job twice.

    The returned dict carries ``stopped_because`` — one of ``"empty"`` (the
    normal "worked through everything" finish), ``"paused"``, ``"locked"``
    (another worker holds the lock) or ``"asked_to_stop"``. Callers MUST use
    it before announcing anything: without it, a drain that did nothing at all
    is indistinguishable from one that processed the whole queue, and the
    caller cheerfully reports "All links processed" over a still-pending
    link. That exact misreport is what the operator hit — three messages at
    once ("started" / "all links processed" / "stopped") while their link had
    never even begun. CLAUDE.md rule 1: never emit output that looks
    successful but isn't.
    """
    announce = announce or N.notify
    t_all = time.time()
    made = 0

    def _summary(reason: str, elapsed: float) -> dict:
        q_now = Q.load_queue(work_dir)
        c_now = Q.counts(q_now)
        return {"made": made, "done": c_now[Q.DONE], "failed": c_now[Q.FAILED],
                "pending": c_now[Q.PENDING], "total_clips": Q.total_clips(q_now),
                "elapsed": elapsed, "stopped_because": reason}

    if not Q.acquire_lock(work_dir):
        log.warning("queue: another worker already holds the lock (queue.lock) — "
                   "skipping this drain to avoid double-rendering a job.")
        return _summary("locked", 0.0)

    reason = "empty"
    # Keep the lock warm while we really are working, so `acquire_lock`'s
    # staleness test can safely reclaim locks that no live worker is behind.
    beat_stop = Q.start_lock_heartbeat(work_dir)
    try:
        while True:
            if should_stop is not None and should_stop():
                reason = "asked_to_stop"
                break
            q = Q.load_queue(work_dir)
            if Q.is_paused(q):       # /pause (or the startup permission gate): stay alive
                reason = "paused"
                break
            job = Q.next_pending(q)
            if job is None:
                break
            idx = q["jobs"].index(job) + 1
            total = len(q["jobs"])
            Q.mutate_queue(work_dir, lambda qq: Q.mark(qq, job["id"], Q.RUNNING))
            log.info("=== queue %d/%d [%s] %s ===", idx, total, job["id"], job["url"])

            # Record the link in flight BEFORE starting: if the power goes out mid-job
            # the next startup can name what it was doing instead of guessing. The
            # prefix is how a crash report can count clips already on disk for THIS
            # job — the queue only records a job's clips on a clean finish.
            detail = Q.describe_settings(job)
            lifecycle.heartbeat(work_dir, current={"url": job["url"], "detail": detail,
                                                   "index": idx, "total": total,
                                                   "prefix": Q.job_slug(job, idx),
                                                   "started": time.time()})
            # Announce the start too — "it's alive and this is what it understood".
            announce(f"🎬 <b>Link {idx}/{total} started</b> — making {detail}\n{job['url'][:80]}")

            log_handler = _attach_job_log(work_dir)
            try:
                ok, n, msg = run_one(job, idx, total, make_cfg, work_dir)
            finally:
                _detach_job_log(log_handler)
            made += n
            lifecycle.heartbeat(work_dir, current=None)
            announce(msg)
            # Between jobs is the only safe moment: nothing is mid-read, and the
            # workspace just used is far too new to be a pruning candidate.
            # No-op unless the operator switched auto-clean on in Settings.
            try:
                from . import maintenance
                maintenance.autoclean(work_dir)
            except Exception as e:  # noqa: BLE001 - housekeeping never fails a run
                log.debug("auto-clean skipped: %s", e)
    finally:
        beat_stop.set()
        Q.release_lock(work_dir)

    return _summary(reason, time.time() - t_all)
