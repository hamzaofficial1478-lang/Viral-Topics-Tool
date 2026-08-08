"""Is ShortForge actually running? — online/offline announcements you can trust.

The bug this exists to kill: the phone said "ShortForge is online and listening"
long after the program had been closed. A notification that is only ever sent on
*start* is not a status, it's a memory — and a stale one is worse than none,
because the operator plans around it.

So the running process keeps a small state file on disk and announces the truth:

* **going offline** — sent on Ctrl+C, on ``SIGTERM``, and (the case that actually
  happens on Windows) on clicking the **X** of the console window, which sends
  ``CTRL_CLOSE_EVENT`` and gives us roughly five seconds before the process is
  killed. Hence the short send timeout: a goodbye that misses the window is
  worse than a terse one that lands.
* **coming back** — on start, a *leftover* state file means the last run died
  without a goodbye (power cut, Task Manager, a hard reboot). That is reported
  explicitly, along with the link it was working on, instead of being silently
  overwritten.

Nothing here is load-bearing for rendering: every failure is swallowed and
logged. A missing notification must never take down a queue.
"""

from __future__ import annotations

import json
import os
import threading
import time

from .utils import log

STATE_FILE = "runstate.json"

# Windows kills a console app ~5s after the X is clicked; leave room to write.
GOODBYE_TIMEOUT = 4

_lock = threading.Lock()
_announced = False          # the goodbye is sent exactly once, whichever path wins
_installed = False


def state_path(work_dir: str = ".shortforge") -> str:
    return os.path.join(work_dir, STATE_FILE)


def read_state(work_dir: str = ".shortforge") -> dict | None:
    try:
        with open(state_path(work_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_state(work_dir: str, state: dict) -> None:
    path = state_path(work_dir)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        log.debug("could not write run state: %s", e)


def clear_state(work_dir: str = ".shortforge") -> None:
    try:
        os.remove(state_path(work_dir))
    except OSError:
        pass


def mark_online(work_dir: str = ".shortforge", mode: str = "queue") -> dict | None:
    """Claim the running state. Returns the PREVIOUS state when the last run
    never said goodbye — that is how a power cut is detected."""
    global _announced
    prev = read_state(work_dir)
    now = time.time()
    _write_state(work_dir, {"pid": os.getpid(), "mode": mode,
                            "started": now, "last_seen": now, "current": None})
    with _lock:
        _announced = False
    return prev


_KEEP = object()      # so `current=None` can mean "cleared", not "unchanged"


def heartbeat(work_dir: str = ".shortforge", current: dict | None = _KEEP) -> None:
    """Refresh 'last seen' and record which link is in flight, so a crash report
    can name it. Pass ``current=None`` to say no job is running."""
    state = read_state(work_dir) or {"pid": os.getpid(), "mode": "queue",
                                     "started": time.time()}
    state["last_seen"] = time.time()
    if current is not _KEEP:
        state["current"] = current
    _write_state(work_dir, state)


def clips_made_for(prefix: str, out_dir: str | None = None) -> int:
    """Best-effort count of clip files already on disk for a job prefix.

    The queue only records a job's clip list on a CLEAN finish, so a crash
    mid-job leaves nothing in queue.json — but the already-rendered .mp4 files
    are real and sitting in the output folder, tagged with the job's prefix
    (``job_slug``). Counting them is the only honest way to answer "how many
    had it made" for a run that ended mid-job."""
    if not prefix:
        return 0
    if out_dir is None:
        try:
            from .config import Config
            out_dir = Config.load().get("paths.output_dir", "out")
        except Exception:  # noqa: BLE001 - a nicety, never a blocker
            out_dir = "out"
    try:
        import glob
        return len(glob.glob(os.path.join(out_dir, f"{prefix}_*.mp4")))
    except OSError:
        return 0


def interrupted_note(prev: dict | None, *, total_clips: int | None = None) -> str:
    """Plain-English description of a run that ended without a goodbye, or ""
    when the last shutdown was clean.

    ``total_clips`` (optional) is the queue-wide tally at the moment of asking
    — "how many videos it had made" overall, not just the interrupted one.
    """
    if not prev:
        return ""
    gap = max(0.0, time.time() - float(prev.get("last_seen") or 0))
    mins = int(gap // 60)
    when = f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago"
    cur = prev.get("current") or {}
    what = f"\nIt was working on: {str(cur.get('url', ''))[:70]}" if cur.get("url") else ""
    if cur.get("detail"):
        what += f" ({cur['detail']})"
    if cur.get("prefix"):
        n = clips_made_for(cur["prefix"])
        if n:
            what += f" — {n} clip(s) already rendered for this link before it stopped"
    made = f"\n📊 {total_clips} clip(s) produced overall before it stopped." if total_clips else ""
    return (f"⚡ The last session ended without shutting down — power cut, forced close "
            f"or a reboot (last seen {when}).{what}{made}\n"
            f"Nothing was lost: unfinished links go back in the queue and clips that "
            f"already rendered are kept.")


def announce_offline(work_dir: str = ".shortforge", reason: str = "closed",
                     notify_fn=None) -> bool:
    """Say we're going down, once. Returns True if this call did the announcing."""
    global _announced
    with _lock:
        if _announced:
            return False
        _announced = True

    extra = ""
    try:
        from . import queue as Q
        q = Q.load_queue(work_dir)
        c = Q.counts(q)
        n = c[Q.PENDING] + c[Q.RUNNING]
        if n:
            extra += (f"\n⏳ {n} link(s) still queued — they resume when ShortForge "
                     f"starts again.")
        total = Q.total_clips(q)
        if total:
            extra += f"\n📊 {total} clip(s) produced so far."
    except Exception:  # noqa: BLE001 - a readable queue is a nicety here, not a must
        pass

    # Read state BEFORE clear_state() below wipes it — name what was in flight,
    # so "it will also tell which video it was making when it got closed" holds
    # even on a clean stop, not just a crash-recovery report on the next start.
    try:
        cur = (read_state(work_dir) or {}).get("current") or {}
        if cur.get("url"):
            extra += f"\n🎬 It was working on: {str(cur['url'])[:70]}"
            if cur.get("detail"):
                extra += f" ({cur['detail']})"
            if cur.get("prefix"):
                n_disk = clips_made_for(cur["prefix"])
                if n_disk:
                    extra += f" — {n_disk} clip(s) already rendered for it"
    except Exception:  # noqa: BLE001
        pass

    text = f"🔴 <b>ShortForge stopped</b> ({reason}).{extra}"
    try:
        if notify_fn is not None:
            notify_fn(text)
        else:
            from .notify import notify
            notify(text, timeout=GOODBYE_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.debug("could not send the shutdown notice: %s", e)
    clear_state(work_dir)
    log.info("shutdown notice sent (%s)", reason)
    return True


# --- catching every way the operator can close the program ------------------- #

_console_handler = None     # ctypes callback; must outlive the call or Windows crashes


def install_exit_notice(work_dir: str = ".shortforge") -> None:
    """Register the shutdown announcement on every exit path we can catch.

    ``atexit`` covers a normal return and Ctrl+C (KeyboardInterrupt still unwinds
    normally). It does NOT cover ``SIGTERM``, and on Windows it does not cover
    closing the console window — so both get explicit handlers.
    """
    global _installed, _console_handler
    if _installed:
        return
    _installed = True

    import atexit
    import signal

    atexit.register(lambda: announce_offline(work_dir, "closed"))

    def _sig(signum, _frame):
        announce_offline(work_dir, "stopped")
        raise SystemExit(0)          # unwinds → atexit runs → guard makes it a no-op

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _sig)
            except (ValueError, OSError):   # not the main thread / unsupported
                pass

    # Windows: clicking the window's X sends CTRL_CLOSE_EVENT and Python installs
    # no handler for it, so the process just vanishes and atexit never runs.
    try:
        import ctypes
        if not hasattr(ctypes, "windll"):
            return
        CTRL_C, CTRL_BREAK, CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN = 0, 1, 2, 5, 6
        reasons = {CTRL_C: "stopped", CTRL_BREAK: "stopped", CTRL_CLOSE: "window closed",
                   CTRL_LOGOFF: "signed out", CTRL_SHUTDOWN: "PC shutting down"}
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _on_console_event(ctrl_type):
            announce_offline(work_dir, reasons.get(int(ctrl_type), "closed"))
            return False        # False = also run the default handler (i.e. exit)

        _console_handler = proto(_on_console_event)     # keep a strong reference
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
        log.debug("shutdown notice armed (including the console X button)")
    except Exception as e:  # noqa: BLE001 - never block startup over a notification
        log.debug("console shutdown handler unavailable: %s", e)


# --- ntfy connectivity, surfaced locally -------------------------------------- #
# A "connection lost" push notification is a contradiction while the connection
# really is lost — it can't be delivered through the channel that's down. The
# honest version: OutageWatch (notify.py) still *attempts* it immediately and
# *guarantees* a "back online" message on recovery, and this records the state
# to disk so the dashboard can show it live even while nothing can be pushed.

def note_ntfy_status(work_dir: str = ".shortforge", ok: bool = True, detail: str = "") -> None:
    """Record ntfy reachability. Writes on the first-ever poll (so there's
    something to show before any failure has happened), on a state change,
    and on every poll while still down (keeps "last checked" fresh during a
    real outage) — but not on every single healthy poll once steady, so a
    good connection doesn't churn the state file every 5s forever."""
    state = read_state(work_dir)
    if state is None:
        return                      # no run in progress; nothing to annotate
    prev_ok = state.get("ntfy_ok")  # None = never recorded yet (fresh state)
    if ok and prev_ok is True:
        return                      # steady-state healthy: nothing changed
    now = time.time()
    state["ntfy_ok"] = bool(ok)
    state["ntfy_checked"] = now
    if not ok and prev_ok is not False:
        state["ntfy_down_since"] = now
    if ok:
        state["ntfy_down_since"] = None
    state["ntfy_detail"] = (detail or "")[:200]
    _write_state(work_dir, state)
