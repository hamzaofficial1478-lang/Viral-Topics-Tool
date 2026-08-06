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
from typing import Any

from .utils import log

QUEUE_FILE = "queue.json"

PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"

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


def apply_job_settings(cfg, job: dict) -> None:
    """Overlay this job's own settings onto a fresh Config (per-link settings)."""
    for key, dotted in JOB_SETTINGS.items():
        if key in job.get("settings", {}):
            cfg.override(dotted, job["settings"][key])


def describe(q: dict) -> str:
    c = counts(q)
    return (f"{len(q.get('jobs', []))} job(s): {c[PENDING]} pending, {c[RUNNING]} running, "
            f"{c[DONE]} done, {c[FAILED]} failed — {total_clips(q)} clip(s) produced")
