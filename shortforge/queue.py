"""Persistent job queue — paste a batch of links, walk them one at a time.

Every state change is written to disk immediately, so a power cut or a closed
laptop never loses the backlog: on restart the runner picks up exactly where it
stopped. A job interrupted mid-render is put back to ``pending``; the pipeline's
own ``render.resume`` then skips the clips that already exist, so a job that died
near the end doesn't start from zero.

Each job carries its OWN settings (how many clips, how long, aspect, resolution),
because "5 x 2min from this link, 6 x 1min from that one" is the real workflow.
"""

from __future__ import annotations

import json
import os
import time
import uuid

from .utils import log

QUEUE_FILE = "queue.json"
LOCK_FILE = "queue.lock"

PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"


# --- single-worker lock ------------------------------------------------------- #
# load_queue/save_queue is a plain read-modify-write with no file lock, so two
# independent drain_queue() runs (e.g. `cli.py queue run` started by hand while
# `cli.py listen`'s worker thread is also draining) can both read the same
# `pending` job before either writes it back `running` — a real TOCTOU window
# that would render the same link twice, including a second paid LLM/dub spend.
# This lock is held for a whole drain_queue() run, not per-job, since the two
# callers that matter are separate PROCESSES, not threads within one.

def lock_path(work_dir: str = ".shortforge") -> str:
    return os.path.join(work_dir, LOCK_FILE)


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check. On any doubt, say alive — stealing a lock
    that's still legitimately held is the dangerous direction to be wrong in."""
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)          # POSIX: signal 0 checks, never actually signals
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def acquire_lock(work_dir: str = ".shortforge") -> bool:
    """Exclusive create; True if acquired. A lock left by a PID that's no
    longer running (crash, kill -9) is reclaimed automatically — the queue
    file's own crash recovery (`requeue_interrupted`) already handles the job
    state, this only clears the stale lock so a restart isn't refused forever."""
    path = lock_path(work_dir)
    os.makedirs(work_dir, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            with open(path, "r", encoding="utf-8") as f:
                held_by = int(f.read().strip())
        except (OSError, ValueError):
            return False           # unreadable lock: don't guess, just refuse
        if _pid_alive(held_by):
            return False           # a live holder — including ourselves — wins
        try:
            os.remove(path)
        except OSError:
            return False
        return acquire_lock(work_dir)   # one retry now that the stale lock is gone


def release_lock(work_dir: str = ".shortforge") -> None:
    try:
        os.remove(lock_path(work_dir))
    except OSError:
        pass

# Per-job overrides: queue setting -> dotted config path applied for that job only.
JOB_SETTINGS = {
    "num_clips": "select.num_clips",
    "duration": "select.target_duration",
    "tolerance": "select.tolerance",
    "aspect": "reframe.aspect",
    "resolution": "reframe.resolution",
    "language": "localize.language",
    "caption_template": "captions.template",
}


def queue_path(work_dir: str = ".shortforge") -> str:
    return os.path.join(work_dir, QUEUE_FILE)


def load_queue(work_dir: str = ".shortforge") -> dict:
    path = queue_path(work_dir)
    if not os.path.isfile(path):
        return {"jobs": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("jobs", [])
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("queue file unreadable (%s); starting empty", e)
        return {"jobs": []}


def save_queue(q: dict, work_dir: str = ".shortforge") -> None:
    """Write atomically — a power cut must never leave a truncated queue file."""
    path = queue_path(work_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def add_job(q: dict, url: str, settings: dict | None = None, label: str = "") -> dict:
    """Append a link with its own settings. Returns the created job."""
    settings = {k: v for k, v in (settings or {}).items()
                if k in JOB_SETTINGS and v not in (None, "")}
    job = {
        "id": uuid.uuid4().hex[:8],
        "url": url.strip(),
        "label": label.strip(),
        "settings": settings,
        "status": PENDING,
        "added": time.time(),
        "started": None,
        "finished": None,
        "clips": [],          # output paths produced by this job
        "error": None,
        "attempts": 0,
    }
    q.setdefault("jobs", []).append(job)
    return job


def get_job(q: dict, job_id: str) -> dict | None:
    return next((j for j in q.get("jobs", []) if j["id"] == job_id), None)


def next_pending(q: dict) -> dict | None:
    """The next job to work on, in the order the links were added."""
    return next((j for j in q.get("jobs", []) if j["status"] == PENDING), None)


def counts(q: dict) -> dict:
    jobs = q.get("jobs", [])
    return {s: sum(1 for j in jobs if j["status"] == s)
            for s in (PENDING, RUNNING, DONE, FAILED)}


def total_clips(q: dict) -> int:
    return sum(len(j.get("clips") or []) for j in q.get("jobs", []))


def requeue_interrupted(q: dict) -> int:
    """Jobs left ``running`` by a crash/power-cut go back to ``pending``.

    Their partial output stays on disk; the pipeline's resume skips clips that
    already rendered, so the job continues rather than restarting."""
    n = 0
    for j in q.get("jobs", []):
        if j["status"] == RUNNING:
            j["status"] = PENDING
            j["error"] = None
            n += 1
    return n


def mark(q: dict, job_id: str, status: str, *, clips: list | None = None,
         error: str | None = None) -> dict | None:
    job = get_job(q, job_id)
    if job is None:
        return None
    job["status"] = status
    if status == RUNNING:
        job["started"] = time.time()
        job["attempts"] = int(job.get("attempts", 0)) + 1
    if status in (DONE, FAILED):
        job["finished"] = time.time()
    if clips is not None:
        job["clips"] = clips
    job["error"] = error
    return job


def job_slug(job: dict, index: int | None = None) -> str:
    """Short, filesystem-safe tag identifying which link an output came from —
    so a folder of clips from ten links is still readable at a glance."""
    import re
    base = job.get("label") or ""
    if not base:
        base = f"link{index:02d}" if index is not None else f"job{job['id']}"
    base = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    return (base or f"job{job['id']}")[:32]


def fmt_duration(seconds) -> str:
    """'90' -> '1m30s'. Phone notifications read better in minutes."""
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return str(seconds)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def describe_settings(job: dict) -> str:
    """Plain-English 'what this link will produce' — used in the phone message
    sent when a link STARTS, so the operator knows the numbers were understood."""
    s = job.get("settings", {}) or {}
    bits: list[str] = []
    n, d = s.get("num_clips"), s.get("duration")
    if n and d:
        bits.append(f"{n} clip(s) of {fmt_duration(d)}")
    elif n:
        bits.append(f"{n} clip(s)")
    elif d:
        bits.append(f"clips of {fmt_duration(d)}")
    for key in ("aspect", "resolution"):
        if s.get(key):
            bits.append(str(s[key]))
    if s.get("language"):
        bits.append(f"→ {s['language']}")
    if s.get("caption_template"):
        bits.append(f"{s['caption_template']} captions")
    return ", ".join(bits) or "default settings"


def apply_job_settings(cfg, job: dict) -> None:
    """Overlay this job's own settings onto a fresh Config (per-link settings)."""
    for key, dotted in JOB_SETTINGS.items():
        if key in job.get("settings", {}):
            cfg.override(dotted, job["settings"][key])


def is_paused(q: dict) -> bool:
    """Paused = keep listening and accepting links, but start no new jobs."""
    return bool(q.get("paused", False))


def set_paused(q: dict, paused: bool) -> None:
    q["paused"] = bool(paused)


def describe(q: dict) -> str:
    c = counts(q)
    return (f"{len(q.get('jobs', []))} job(s): {c[PENDING]} pending, {c[RUNNING]} running, "
            f"{c[DONE]} done, {c[FAILED]} failed — {total_clips(q)} clip(s) produced")
