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


def clear_cache(work_dir: str, what: str = "all") -> int:
    """Remove cached artifacts under ``work_dir`` (A2 cache controls).

    ``what``: "translation" (transcript_<lang>_*.json), "transcript"
    (transcript.json), or "all" (the whole work dir). Returns files removed.
    """
    import glob
    import shutil

    if not os.path.isdir(work_dir):
        return 0
    removed = 0
    if what == "all":
        for entry in os.listdir(work_dir):
            p = os.path.join(work_dir, entry)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
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
