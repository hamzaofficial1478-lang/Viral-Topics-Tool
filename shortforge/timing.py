"""STEP 0 — per-stage + per-ffmpeg timing, so optimization targets the real
bottleneck rather than the assumed one.

A single :class:`Timings` is made active for the duration of a pipeline run
(:func:`activate`). Stage timers (:func:`stage`) and the shared command runner
append to whichever ``Timings`` is active on the current context; when none is
active every call is a cheap no-op, so unit tests and one-off CLI commands are
unaffected. Uses a ``ContextVar`` so it stays correct as stages move onto
threads (parallel render/scoring) later.
"""

from __future__ import annotations

import contextlib
import time
from contextvars import ContextVar

_active: ContextVar["Timings | None"] = ContextVar("shortforge_timings", default=None)


class Timings:
    """Ordered per-stage durations (accumulated by name) + every ffmpeg call."""

    def __init__(self) -> None:
        self._t0 = time.time()
        self.stages: list[dict] = []       # [{stage, seconds, note}]
        self.ffmpeg: list[dict] = []       # [{seconds, cmd}]

    def add(self, name: str, seconds: float | None = None,
            note: str | None = None) -> None:
        """Add ``seconds`` to stage ``name`` (creating it in order the first
        time). ``seconds=None`` records a stage that ran no timed work (e.g. a
        skipped stage); ``note`` annotates it (``cached`` / ``skipped`` / …)."""
        for s in self.stages:
            if s["stage"] == name:
                if seconds is not None:
                    s["seconds"] = round((s.get("seconds") or 0.0) + seconds, 1)
                if note:
                    s["note"] = note
                return
        self.stages.append({
            "stage": name,
            "seconds": round(seconds, 1) if seconds is not None else None,
            "note": note,
        })

    def record_ffmpeg(self, cmd, seconds: float) -> None:
        c = " ".join(str(x) for x in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        self.ffmpeg.append({"seconds": round(seconds, 2), "cmd": c})

    def total_seconds(self) -> float:
        return time.time() - self._t0

    def summary_line(self) -> str:
        parts = []
        for s in self.stages:
            secs, note = s.get("seconds"), s.get("note")
            if secs is None:
                parts.append(f"{s['stage']} {note or 'skipped'}")
            elif note:
                parts.append(f"{s['stage']} {secs:.0f}s ({note})")
            else:
                parts.append(f"{s['stage']} {secs:.0f}s")
        return "timings: " + " | ".join(parts) + f"  — total {self.total_seconds():.0f}s"

    def to_dict(self) -> dict:
        return {
            "total_seconds": round(self.total_seconds(), 1),
            "stages": [dict(s) for s in self.stages],
            "ffmpeg_calls": [dict(f) for f in self.ffmpeg],
        }


def activate(t: "Timings | None"):
    """Make ``t`` the active Timings; returns a token for :func:`deactivate`."""
    return _active.set(t)


def deactivate(token) -> None:
    try:
        _active.reset(token)
    except (ValueError, LookupError):
        pass


def current() -> "Timings | None":
    return _active.get()


@contextlib.contextmanager
def stage(name: str, note: str | None = None):
    """Time a block, adding it to stage ``name`` on the active Timings (no-op if
    none is active)."""
    t = _active.get()
    if t is None:
        yield
        return
    t0 = time.time()
    try:
        yield
    finally:
        t.add(name, time.time() - t0, note=note)


def note(name: str, text: str) -> None:
    """Annotate a stage (e.g. mark it ``cached`` / ``skipped``) without timing."""
    t = _active.get()
    if t is not None:
        t.add(name, None, note=text)


def record_ffmpeg(cmd, seconds: float) -> None:
    t = _active.get()
    if t is not None:
        t.record_ffmpeg(cmd, seconds)
