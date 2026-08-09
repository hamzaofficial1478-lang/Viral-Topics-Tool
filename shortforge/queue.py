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
import threading
import time
import uuid

from .utils import log

QUEUE_FILE = "queue.json"
LOCK_FILE = "queue.lock"

# A held lock is kept "warm" by the worker touching its mtime (see
# `lock_heartbeat`). Anything older than this has no live worker behind it and
# is reclaimed, whatever its PID says.
#
# Why an age check and not PID liveness alone: PID liveness was the ONLY test,
# and Windows recycles PIDs aggressively. A worker killed without releasing
# (window closed, kill -9, power cut) leaves the file behind; once an unrelated
# process inherits that number, `pid_alive()` answers True forever and the
# queue is wedged permanently — every drain returns "locked", links pile up
# pending, and nothing in the dashboard or ntfy can clear it. That is not
# hypothetical: it is the state the operator's machine ended up in.
LOCK_STALE_AFTER = 300.0
_LOCK_TOUCH_EVERY = 30.0

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


def acquire_lock(work_dir: str = ".shortforge") -> bool:
    """Exclusive create; True if acquired. A lock left by a PID that's no
    longer running (crash, kill -9) is reclaimed automatically — the queue
    file's own crash recovery (`requeue_interrupted`) already handles the job
    state, this only clears the stale lock so a restart isn't refused forever."""
    from .utils import pid_alive

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
            held_by = -1           # unreadable/corrupt: fall through to the age test
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:            # vanished between the two calls — it's free now
            return acquire_lock(work_dir)
        if age < LOCK_STALE_AFTER and held_by > 0 and pid_alive(held_by):
            return False           # a live holder, still checking in — it wins
        why = ("its holder is gone" if held_by <= 0 or not pid_alive(held_by)
               else f"it stopped checking in {age:.0f}s ago (PID {held_by} was reused)")
        log.warning("queue: reclaiming a stale lock — %s. If a real worker is "
                    "somehow still running it will stop at its next job.", why)
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


def touch_lock(work_dir: str = ".shortforge") -> None:
    """Mark the held lock as still alive. Cheap, best-effort."""
    try:
        os.utime(lock_path(work_dir), None)
    except OSError:
        pass


def start_lock_heartbeat(work_dir: str = ".shortforge",
                         every: float = _LOCK_TOUCH_EVERY) -> threading.Event:
    """Keep the held lock warm while this worker really is working.

    Returns the stop Event — ``.set()`` it when the drain finishes.

    The age test in `acquire_lock` is only safe because a live worker keeps
    saying so. A single job can legitimately run far longer than
    ``LOCK_STALE_AFTER`` (a 3-minute clip on this CPU is minutes of encode) and
    `drain_queue` has no hook inside a job to touch the file from, so a daemon
    thread does it on a timer. A process that dies stops touching instantly,
    and its lock ages out by itself — which is the whole point: no leftover
    lock can wedge the queue forever again.
    """
    stop = threading.Event()

    def _beat():
        while not stop.wait(every):
            touch_lock(work_dir)

    threading.Thread(target=_beat, daemon=True, name="queue-lock-heartbeat").start()
    return stop

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


# --- duplicate links ---------------------------------------------------------- #
# Adding the same video twice renders it twice: hours of CPU on this machine and
# a second helping of paid LLM/TTS spend, for a byte-identical result. Nothing
# checked for it. Plain string comparison is not enough either — sharing from the
# YouTube phone app appends a `?si=` tracking parameter, so the *same* video
# arrives as a different string every time you share it.

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
             "youtu.be", "www.youtu.be"}
# Share/analytics junk that never identifies a different video.
_JUNK_PARAMS = {"si", "feature", "t", "start", "utm_source", "utm_medium",
                "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid",
                "pp", "ab_channel", "app"}


def canonical_url(url: str) -> str:
    """A stable identity for a link, so the same video is recognised however
    it was shared. YouTube links reduce to their video id; anything else is
    normalised (lowercased host, no trailing slash, tracking params dropped).
    """
    from urllib.parse import parse_qs, urlparse, urlencode

    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
    except ValueError:                      # malformed — compare it literally
        return raw.lower()
    host = (p.hostname or "").lower()
    path = (p.path or "").rstrip("/")

    if host in _YT_HOSTS:
        vid = ""
        if host.endswith("youtu.be"):
            vid = path.lstrip("/").split("/")[0]
        elif path == "/watch":
            vid = (parse_qs(p.query).get("v") or [""])[0]
        else:
            for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
                if path.startswith(prefix):
                    vid = path[len(prefix):].split("/")[0]
                    break
        if vid:
            return f"youtube:{vid}"          # one identity for every share form

    keep = {k: v for k, v in parse_qs(p.query).items() if k.lower() not in _JUNK_PARAMS}
    query = urlencode(sorted((k, v[0]) for k, v in keep.items() if v))
    return f"{host}{path}" + (f"?{query}" if query else "")


def find_duplicate(q: dict, url: str) -> dict | None:
    """The existing job for this link, whatever status it's in, or None."""
    target = canonical_url(url)
    if not target:
        return None
    return next((j for j in q.get("jobs", [])
                 if canonical_url(j.get("url", "")) == target), None)


def add_links(q: dict, urls, settings: dict | None = None,
              label: str = "") -> tuple[list[dict], list[dict]]:
    """Add several links at once, skipping ones already in the queue.

    Returns ``(added, skipped)`` where ``skipped`` holds the *existing* jobs the
    new links collided with. Shared by every door — ntfy, the dashboard's Add
    button, the Chat screen and `cli.py queue add` — so all four dedupe the same
    way and report it in the same words (CLAUDE.md rule 6); four private copies
    of this policy would just be four chances to disagree.

    Duplicates within the same batch are caught too, since each addition is
    checked against the queue as it grows.
    """
    added: list[dict] = []
    skipped: list[dict] = []
    for url in urls:
        existing = find_duplicate(q, url)
        if existing is not None:
            skipped.append(existing)
            continue
        added.append(add_job(q, url, settings, label=label))
    return added, skipped


def describe_skipped(skipped: list[dict]) -> str:
    """Plain-English 'why nothing happened for those', with the way out."""
    if not skipped:
        return ""
    states = {PENDING: "already waiting", RUNNING: "running right now",
              DONE: "already done", FAILED: "already tried (it failed)"}
    lines = [f"• {j['url'][:60]} — {states.get(j['status'], j['status'])}"
             for j in skipped[:5]]
    more = f"\n…and {len(skipped) - 5} more" if len(skipped) > 5 else ""
    return ("\n".join(lines) + more
            + "\nTo run one again, remove it first (/clear drops finished jobs).")


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
