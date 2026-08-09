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
# The dashboard (`cli.py ui`) is a separate OS process from the worker
# (`cli.py listen` / `queue run`) and must never share the worker's state
# file: mark_online() unconditionally overwrites pid/mode/current, so if the
# UI process wrote to the SAME file it would stomp the worker's own in-flight
# job record — corrupting the exact crash-recovery detail (interrupted_note)
# that feature depends on. A separate file makes the two trackers independent
# by construction, not by convention.
UI_STATE_FILE = "ui_runstate.json"

# Windows kills a console app ~5s after the X is clicked; leave room to write.
GOODBYE_TIMEOUT = 4

_lock = threading.Lock()
_announced = False          # the goodbye is sent exactly once, whichever path wins
_installed = False


def state_path(work_dir: str = ".shortforge", state_file: str = STATE_FILE) -> str:
    return os.path.join(work_dir, state_file)


def read_state(work_dir: str = ".shortforge", state_file: str = STATE_FILE) -> dict | None:
    try:
        with open(state_path(work_dir, state_file), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_state(work_dir: str, state: dict, state_file: str = STATE_FILE) -> None:
    from .utils import write_json_atomic
    try:
        write_json_atomic(state_path(work_dir, state_file), state)
    except OSError as e:
        log.debug("could not write run state: %s", e)


def clear_state(work_dir: str = ".shortforge", state_file: str = STATE_FILE, *,
                only_if_mine: bool = False) -> None:
    """Remove the state file. With ``only_if_mine``, leave it alone unless it
    still names THIS process.

    Why that guard exists: `cli.py queue run` (which the dashboard can spawn as
    a short helper) and `cli.py listen` share ``runstate.json``. The helper's
    atexit used to delete it unconditionally — wiping the *listener's* live
    state, including the record of whichever link was in flight. That silently
    broke the dashboard's "is a worker running?" check, the duplicate-instance
    guard, and crash recovery, all from a process that had merely finished its
    own short run. A process may only retract its own claim.
    """
    path = state_path(work_dir, state_file)
    if only_if_mine:
        state = read_state(work_dir, state_file)
        if state is not None and state.get("pid") != os.getpid():
            log.debug("not clearing %s — it belongs to pid %s, not us (%s)",
                      state_file, state.get("pid"), os.getpid())
            return
    try:
        os.remove(path)
    except OSError:
        pass


def cancel_goodbye(work_dir: str = ".shortforge", state_file: str = STATE_FILE) -> None:
    """Retract this process's run-state without announcing a shutdown.

    For a run that exited immediately having done nothing — the queue was
    paused, or another worker already held the lock. It genuinely stopped, but
    saying "🔴 ShortForge stopped" on top of "queue is paused, nothing started"
    reads as the whole program going down (the operator had the dashboard open
    and the listener running at the time). The state file is still cleared, so
    the next start doesn't misread the leftover as a power cut.
    """
    global _announced
    with _lock:
        _announced = True          # makes the atexit/signal handlers a no-op
    clear_state(work_dir, state_file, only_if_mine=True)


def mark_online(work_dir: str = ".shortforge", mode: str = "queue",
                state_file: str = STATE_FILE) -> dict | None:
    """Claim the running state. Returns the PREVIOUS state when the last run
    never said goodbye — that is how a power cut is detected."""
    global _announced
    prev = read_state(work_dir, state_file)
    now = time.time()
    _write_state(work_dir, {"pid": os.getpid(), "mode": mode,
                            "started": now, "last_seen": now, "current": None}, state_file)
    with _lock:
        _announced = False
    return prev


_KEEP = object()      # so `current=None` can mean "cleared", not "unchanged"


def heartbeat(work_dir: str = ".shortforge", current: dict | None = _KEEP,
             state_file: str = STATE_FILE) -> None:
    """Refresh 'last seen' and record which link is in flight, so a crash report
    can name it. Pass ``current=None`` to say no job is running."""
    state = read_state(work_dir, state_file) or {"pid": os.getpid(), "mode": "queue",
                                                  "started": time.time()}
    state["last_seen"] = time.time()
    if current is not _KEEP:
        state["current"] = current
    _write_state(work_dir, state, state_file)


def worker_is_live(work_dir: str = ".shortforge", *, freshness_s: float = 30.0) -> bool:
    """Is a queue worker (``cli.py listen``) running and checking in?

    ``listen`` heartbeats every 5s even while idle, so a recent ``last_seen``
    means its worker thread is alive and will pick up pending work by itself
    within a few seconds of the queue being unpaused. A missing or stale file
    means nothing is going to run, however healthy the queue looks.

    Shared by the dashboard (deciding whether to spawn a worker) and by
    ``/status`` (telling the operator why nothing is happening) so the two can
    never give contradictory answers about the same machine.
    """
    state = read_state(work_dir)
    if not state:
        return False
    return (time.time() - float(state.get("last_seen") or 0)) < freshness_s


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
                     notify_fn=None, *, state_file: str = STATE_FILE,
                     icon: str = "🔴", title: str = "ShortForge stopped",
                     include_queue_detail: bool = True) -> bool:
    """Say we're going down, once. Returns True if this call did the announcing.

    ``include_queue_detail=False`` is for the dashboard's own channel (see
    ``UI_STATE_FILE``): the UI closing says nothing about whether the queue
    worker is still running, so attaching "N clip(s) produced" / "it was
    working on X" to *that* message would misrepresent it as the whole
    program stopping when only the window did.
    """
    global _announced
    with _lock:
        if _announced:
            return False
        _announced = True

    extra = ""
    if include_queue_detail:
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
            cur = (read_state(work_dir, state_file) or {}).get("current") or {}
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

    text = f"{icon} <b>{title}</b> ({reason}).{extra}"
    try:
        if notify_fn is not None:
            notify_fn(text)
        else:
            from .notify import notify
            notify(text, timeout=GOODBYE_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.debug("could not send the shutdown notice: %s", e)
    # only_if_mine: a short-lived `queue run` helper exiting must never delete
    # a still-running listener's state file (see clear_state's docstring).
    clear_state(work_dir, state_file, only_if_mine=True)
    log.info("shutdown notice sent (%s)", reason)
    return True


# --- catching every way the operator can close the program ------------------- #

_console_handler = None     # ctypes callback; must outlive the call or Windows crashes
_session_wndproc = None     # ditto for the shutdown window's WNDPROC

# Windows shutdown/logoff messages. An interactive program learns the PC is
# going down through its WINDOW, not through the console control handler.
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
ENDSESSION_LOGOFF = 0x80000000


def session_end_reason(msg: int, lparam: int) -> str | None:
    """Map a Windows session message to a shutdown reason, or None if it isn't one.

    Pulled out as a pure function so the routing is testable off Windows — the
    surrounding window plumbing can only be exercised on the operator's machine.
    """
    if msg not in (WM_QUERYENDSESSION, WM_ENDSESSION):
        return None
    return "signed out" if lparam & ENDSESSION_LOGOFF else "PC shutting down"


def install_session_end_notice(on_end) -> bool:
    """Catch a Windows shutdown/logoff, which the console handler cannot see.

    ``SetConsoleCtrlHandler`` looks like it covers this — the existing handler
    maps ``CTRL_SHUTDOWN_EVENT`` and ``CTRL_LOGOFF_EVENT`` — but Microsoft's own
    HandlerRoutine documentation is explicit that both are delivered *only to
    services*: "Interactive applications are not present by the time the system
    sends this signal." A console program started from a .bat file is
    interactive, so those two branches have never once run. That is exactly the
    operator's report: closing the window sends a goodbye (CTRL_CLOSE_EVENT is
    real), shutting the PC down sends nothing.

    What interactive programs actually get is ``WM_QUERYENDSESSION`` /
    ``WM_ENDSESSION``, delivered to a top-level window — so this creates a
    hidden one and pumps messages for it on a daemon thread. It must be a real
    top-level window: a message-only (``HWND_MESSAGE``) window is excluded from
    these broadcasts.

    Returns True if the listener was armed. Never raises: a missing goodbye is
    a nuisance, a crashed startup is not acceptable.
    """
    global _session_wndproc
    try:
        import ctypes
        from ctypes import wintypes
        if not hasattr(ctypes, "windll"):
            return False                     # not Windows; nothing to arm
    except Exception:                        # noqa: BLE001 - pragma: no cover
        return False

    try:
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                     ctypes.c_size_t, ctypes.c_ssize_t)
        user32 = ctypes.windll.user32

        def _proc(hwnd, msg, wparam, lparam):
            reason = session_end_reason(int(msg), int(lparam))
            if reason is not None:
                try:
                    on_end(reason)
                except Exception:            # noqa: BLE001 - never refuse shutdown
                    pass
                # TRUE = "fine by us". Blocking the shutdown to buy time would
                # hold up the operator's PC; the notify attempt already happened
                # synchronously above, inside the window Windows gives us.
                return LRESULT(1).value if msg == WM_QUERYENDSESSION else LRESULT(0).value
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        _session_wndproc = WNDPROC(_proc)    # strong ref: GC here would crash Windows

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        def _pump():
            wc = WNDCLASS()
            wc.lpfnWndProc = _session_wndproc
            wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
            wc.lpszClassName = "ShortForgeSessionWatcher"
            if not user32.RegisterClassW(ctypes.byref(wc)):
                return
            hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "ShortForge",
                                          0, 0, 0, 0, 0, None, None, wc.hInstance, None)
            if not hwnd:
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        threading.Thread(target=_pump, daemon=True,
                         name="shortforge-session-watcher").start()
        log.debug("shutdown notice armed for PC shutdown / sign-out (window messages)")
        return True
    except Exception as e:                   # noqa: BLE001 - never block startup
        log.debug("session-end listener unavailable: %s", e)
        return False


def install_exit_notice(work_dir: str = ".shortforge", *, state_file: str = STATE_FILE,
                        icon: str = "🔴", title: str = "ShortForge stopped",
                        include_queue_detail: bool = True) -> None:
    """Register the shutdown announcement on every exit path we can catch.

    ``atexit`` covers a normal return and Ctrl+C (KeyboardInterrupt still unwinds
    normally). It does NOT cover ``SIGTERM``, and on Windows it does not cover
    closing the console window — so both get explicit handlers.

    The ``state_file``/``icon``/``title``/``include_queue_detail`` kwargs let
    the dashboard process (``cli.py ui``) arm its OWN independent shutdown
    notice — separate state file (never touches the worker's ``runstate.json``),
    different wording (closing the dashboard window says nothing about whether
    the queue worker is still running). Each OS process only ever calls this
    once for one channel, so the module-level ``_installed``/``_announced``
    guards below don't need to be channel-aware themselves.
    """
    global _installed, _console_handler
    if _installed:
        return
    _installed = True

    import atexit
    import signal

    def _offline(reason: str) -> bool:
        return announce_offline(work_dir, reason, state_file=state_file, icon=icon,
                                title=title, include_queue_detail=include_queue_detail)

    atexit.register(lambda: _offline("closed"))

    def _sig(signum, _frame):
        _offline("stopped")
        raise SystemExit(0)          # unwinds → atexit runs → guard makes it a no-op

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _sig)
            except (ValueError, OSError):   # not the main thread / unsupported
                pass

    # The case the console handler structurally cannot see: the operator shuts
    # the PC down (or signs out) with ShortForge still running. Armed FIRST —
    # the Windows block below returns early on other platforms, and burying this
    # after it made a no-op out of the very thing that fixes the reported bug.
    install_session_end_notice(_offline)

    # Windows: clicking the window's X sends CTRL_CLOSE_EVENT and Python installs
    # no handler for it, so the process just vanishes and atexit never runs.
    try:
        import ctypes
        if not hasattr(ctypes, "windll"):
            return
        # Only the first three are real here. CTRL_LOGOFF_EVENT (5) and
        # CTRL_SHUTDOWN_EVENT (6) are documented as delivered to SERVICES only —
        # an interactive console app is already gone by the time Windows sends
        # them — so they were listed here but never once fired, which is why a
        # PC shutdown produced no goodbye. install_session_end_notice() below
        # covers that case properly, via window messages.
        CTRL_C, CTRL_BREAK, CTRL_CLOSE = 0, 1, 2
        reasons = {CTRL_C: "stopped", CTRL_BREAK: "stopped", CTRL_CLOSE: "window closed"}
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _on_console_event(ctrl_type):
            _offline(reasons.get(int(ctrl_type), "closed"))
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
