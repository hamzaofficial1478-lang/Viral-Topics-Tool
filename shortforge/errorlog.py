"""One shared log of every warning and error, for the 🐛 Error Logs screen.

The dashboard, the long-video worker and the Shorts downloader are separate
processes; each appends here, tagged with where the problem came from.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time

FILE = "errors.jsonl"
LONG, SHORTS, APP = "long", "shorts", "app"
LABELS = {LONG: "📹 Long videos", SHORTS: "📥 Shorts", APP: "⚙️ App"}
_MAX_BYTES = 1_000_000
_KEEP_LINES = 800

_tl = threading.local()
_default = {"source": APP}
_write_lock = threading.Lock()


def path(work_dir: str) -> str:
    return os.path.join(work_dir, FILE)


def current() -> str:
    return getattr(_tl, "source", None) or _default["source"]


def set_current(src: str | None) -> None:
    _tl.source = src


@contextlib.contextmanager
def source(name: str):
    """Tag everything logged on this thread inside the block as ``name``."""
    prev = getattr(_tl, "source", None)
    _tl.source = name
    try:
        yield
    finally:
        _tl.source = prev


def _trim(p: str) -> None:
    try:
        if os.path.getsize(p) <= _MAX_BYTES:
            return
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-_KEEP_LINES:]
        with open(p, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError:
        pass


def append(work_dir: str, level: str, message: str, src: str | None = None) -> None:
    entry = {"t": time.time(), "level": level, "source": src or current(),
             "msg": (message or "")[:3000]}
    p = path(work_dir)
    with _write_lock:
        try:
            os.makedirs(work_dir, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _trim(p)
        except OSError:
            pass


class Handler(logging.Handler):
    def __init__(self, work_dir: str):
        super().__init__(level=logging.WARNING)
        self.work_dir = work_dir

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        append(self.work_dir, record.levelname, msg)


def install(work_dir: str, default_source: str = APP) -> None:
    """Send this process's warnings/errors to the shared log (idempotent)."""
    _default["source"] = default_source
    logger = logging.getLogger("shortforge")
    target = os.path.abspath(path(work_dir))
    for h in logger.handlers:
        if isinstance(h, Handler) and os.path.abspath(path(h.work_dir)) == target:
            return
    logger.addHandler(Handler(work_dir))


def uninstall() -> None:
    logger = logging.getLogger("shortforge")
    for h in [h for h in logger.handlers if isinstance(h, Handler)]:
        logger.removeHandler(h)
    _default["source"] = APP


def read(work_dir: str, limit: int = 300) -> list[dict]:
    """Newest first."""
    try:
        with open(path(work_dir), "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def clear(work_dir: str) -> None:
    with _write_lock:
        try:
            os.remove(path(work_dir))
        except OSError:
            pass
