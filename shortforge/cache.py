"""Caching layer (gap #12) keyed by source content hash.

Transcription and scene analysis are the expensive, deterministic steps; caching
them means every language/style variant of a source reuses one transcribe pass.
Phase 1 caches the transcript; the same store is where Phase 3 dubs will live.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .utils import log


class Cache:
    def __init__(self, work_dir: str, source_hash: str):
        self.dir = os.path.join(work_dir, source_hash)
        os.makedirs(self.dir, exist_ok=True)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def has(self, name: str) -> bool:
        return os.path.isfile(self.path(name))

    def load_json(self, name: str) -> Any | None:
        p = self.path(name)
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                log.debug("cache hit: %s", p)
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("cache read failed, ignoring: %s", p)
            return None

    def save_json(self, name: str, data: Any) -> None:
        p = self.path(name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.debug("cache write: %s", p)


def protected_names() -> set[str]:
    """Files in the work dir that are OPERATIONAL STATE, not cache.

    The work dir holds both, side by side: re-derivable caches (per-source-hash
    transcript dirs, downloads, hook scores) and the program's live state — the
    job queue, the run/heartbeat files, the chat history, the queue lock, the
    current job's log.

    `clear_cache(work_dir, "all")` used to delete every entry in the directory
    indiscriminately, so `python cli.py cache clear` — the command an operator
    reaches for precisely when the disk is filling — destroyed the entire
    pending queue, the worker's run state and the chat log, to reclaim space
    that was almost entirely in the caches it was actually aiming at. Deleting
    the lock and run state under a live worker also breaks the double-render
    guard and crash recovery.

    Imported lazily from the modules that own each name so this can never drift
    out of sync with them (and to avoid an import cycle: runner -> pipeline ->
    cache).
    """
    from . import lifecycle, queue as Q, remote_control as RC
    from . import runner

    return {Q.QUEUE_FILE, Q.LOCK_FILE, lifecycle.STATE_FILE,
            lifecycle.UI_STATE_FILE, RC.CHAT_LOG_FILE, runner.JOB_LOG_FILE}


def is_protected(entry: str) -> bool:
    """True for a state file or an in-flight atomic-write temp of one.

    Atomic writes land on a per-writer sibling named ``<file>.<random>.tmp``
    (a shared ``<file>.tmp`` let concurrent writers corrupt each other), so the
    guard matches by prefix rather than by exact name.
    """
    if entry in protected_names():
        return True
    return entry.endswith(".tmp") and any(
        entry.startswith(name + ".") for name in protected_names())


def clear_cache(work_dir: str, what: str = "all") -> int:
    """Remove cached artifacts under ``work_dir`` (A2 cache controls).

    ``what``: "translation" (transcript_<lang>_*.json), "transcript"
    (transcript.json), or "all" (every cache, but never the operational state
    files — see :func:`protected_names`). Returns files removed.
    """
    import glob
    import shutil

    if not os.path.isdir(work_dir):
        return 0
    removed = 0
    if what == "all":
        for entry in os.listdir(work_dir):
            if is_protected(entry):
                continue
            p = os.path.join(work_dir, entry)
            try:
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
            except OSError:
                continue
            removed += 1
        return removed
    if what == "translation":
        pattern = "transcript_*.json"
    elif what == "transcript":
        pattern = "transcript.json"
    else:
        return 0
    for hash_dir in glob.glob(os.path.join(work_dir, "*")):
        if not os.path.isdir(hash_dir):
            continue
        for f in glob.glob(os.path.join(hash_dir, pattern)):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
    return removed
