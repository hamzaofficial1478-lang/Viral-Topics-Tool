"""ntfy.sh notifications + keeping Windows awake during long runs.

ntfy is the ONLY remote-control/notification channel (Telegram was removed —
ntfy needs no account/bot setup, is a plain HTTPS POST to an ordinary host, and
has free phone apps, so keeping both bought nothing). Credentials live in the
same gitignored provider store as the API keys (set them in Settings →
Notifications), never in the repo. Every send is best-effort: a failed
notification must never take down a render that is otherwise fine.
"""

from __future__ import annotations

import urllib.request

from .utils import log


def configured() -> bool:
    return bool(_ntfy_topic())


def _ntfy_topic() -> str | None:
    try:
        from .providers.store import load_store
        return (load_store().get("ntfy", {}) or {}).get("topic") or None
    except Exception:  # noqa: BLE001
        return None


def _ntfy_server() -> str:
    try:
        from .providers.store import load_store
        return ((load_store().get("ntfy", {}) or {}).get("server")
                or "https://ntfy.sh").rstrip("/")
    except Exception:  # noqa: BLE001
        return "https://ntfy.sh"


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text or "")


def send_ntfy(text: str, topic: str | None = None, server: str | None = None,
              timeout: int = 20) -> tuple[bool, str]:
    """POST a notification to ntfy.sh. Returns (ok, detail); never raises."""
    topic = topic or _ntfy_topic()
    if not topic:
        return False, "ntfy not configured"
    url = f"{(server or _ntfy_server())}/{topic}"
    try:
        from .netdiag import build_opener
        body = _strip_html(text)[:3000].encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Title": "ShortForge",
                                              "Content-Type": "text/plain; charset=utf-8"})
        with build_opener().open(req, timeout=timeout) as r:
            return (200 <= r.status < 300), f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def test_ntfy(topic: str, server: str = "https://ntfy.sh") -> tuple[bool, str]:
    ok, detail = send_ntfy("✅ ShortForge is connected. Progress updates arrive here.",
                           topic=topic, server=server)
    return (ok, "Sent — check the ntfy app (or the topic page in your browser)."
            if ok else f"Failed: {detail}")


def notify(text: str, *, timeout: int = 20) -> bool:
    """Fire-and-forget notification over ntfy. Never raises.

    ``timeout`` is short on the shutdown path: Windows gives a closing console
    about five seconds before it kills the process.
    """
    if not _ntfy_topic():
        return False
    ok, detail = send_ntfy(text, timeout=timeout)
    if not ok:
        log.warning("ntfy notify failed: %s", detail)
    return ok


# --- network outages -------------------------------------------------------- #

class OutageWatch:
    """Notice when the machine loses its connection, and say so when it returns.

    Deliberate honesty about what is possible: ntfy is the only channel, so
    while it is unreachable an alert genuinely cannot be delivered — that is
    the definition of the problem. The outage is *attempted* immediately
    anyway (harmless if it fails) and *reported for certain* on recovery, with
    how long the gap was. ``ntfy_bot.listen`` also records the down state
    locally (``lifecycle.note_ntfy_status``) so the dashboard can show it even
    while no push can get through.

    A single dropped poll is normal on any connection, so an outage is only
    declared after ``threshold`` consecutive failures.
    """

    def __init__(self, what: str = "the network", threshold: int = 3, notify_fn=None):
        self.what = what
        self.threshold = max(1, threshold)
        self._notify = notify_fn or notify
        self._fails = 0
        self._down_since: float | None = None

    @property
    def down(self) -> bool:
        return self._down_since is not None

    def record(self, ok: bool, detail: str = "") -> str | None:
        """Feed one poll result. Returns the message sent, or None."""
        import time
        if ok:
            if self._down_since is None:
                self._fails = 0
                return None
            mins = max(1, int((time.time() - self._down_since) // 60))
            self._down_since = None
            self._fails = 0
            msg = (f"🌐 <b>Back online</b> — {self.what} was unreachable for about "
                   f"{mins} min. Nothing was lost; the queue kept its place.")
            self._notify(msg)
            log.info("network recovered after ~%d min", mins)
            return msg

        self._fails += 1
        if self._down_since is not None or self._fails < self.threshold:
            return None
        self._down_since = time.time()
        msg = (f"⚠️ <b>Network problem</b> — can't reach {self.what} "
               f"({self._fails} tries failed).\n"
               f"{detail[:200]}\n"
               f"Rendering carries on; phone commands resume when the connection does.")
        self._notify(msg)          # may itself fail — that is exactly the situation
        log.warning("network: %s unreachable after %d attempts", self.what, self._fails)
        return msg


# --- keep the machine working while the screen sleeps ----------------------- #

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040


class KeepAwake:
    """Stop Windows suspending the machine mid-render.

    The screen may still blank (that's fine and saves power) but the SYSTEM stays
    awake, so a queue keeps rendering with the monitor off. No-op off Windows.
    Use as a context manager around long work.
    """

    def __init__(self, reason: str = "ShortForge is rendering"):
        self.reason = reason
        self._active = False

    def __enter__(self):
        try:
            import ctypes
            if not hasattr(ctypes, "windll"):
                return self          # not Windows
            flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED
            if ctypes.windll.kernel32.SetThreadExecutionState(flags) == 0:
                # Away-mode is refused on some editions; retry without it.
                flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
                ctypes.windll.kernel32.SetThreadExecutionState(flags)
            self._active = True
            log.info("power: sleep suppressed while working (screen may still turn off)")
        except Exception as e:  # noqa: BLE001
            log.debug("could not suppress sleep: %s", e)
        return self

    def __exit__(self, *exc):
        if not self._active:
            return False
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)  # release
        except Exception:  # noqa: BLE001
            pass
        return False
