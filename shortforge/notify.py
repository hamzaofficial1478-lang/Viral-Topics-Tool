"""Telegram notifications + keeping Windows awake during long runs.

Credentials live in the same gitignored provider store as the API keys (set them
in Settings → Telegram), never in the repo. Every send is best-effort: a failed
notification must never take down a render that is otherwise fine.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .utils import log

_API = "https://api.telegram.org/bot{token}/{method}"


def _creds() -> tuple[str | None, str | None]:
    """(bot_token, chat_id) from the provider store."""
    try:
        from .providers.store import load_store
        tg = load_store().get("telegram", {}) or {}
        return (tg.get("bot_token") or None), (str(tg.get("chat_id")) if tg.get("chat_id") else None)
    except Exception:  # noqa: BLE001
        return None, None


def configured() -> bool:
    token, chat = _creds()
    return bool(token and chat) or bool(_ntfy_topic())


# --- ntfy.sh fallback ------------------------------------------------------- #
# Telegram is IP-blocked by some ISPs/countries: DNS resolves but packets to its
# IPs are dropped, so no amount of retrying helps. ntfy.sh is a plain HTTPS POST
# to an ordinary host, needs no account or token, and has free phone apps — so it
# still delivers where Telegram cannot.

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


def send_ntfy(text: str, topic: str | None = None, server: str | None = None) -> tuple[bool, str]:
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
        with build_opener().open(req, timeout=20) as r:
            return (200 <= r.status < 300), f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def test_ntfy(topic: str, server: str = "https://ntfy.sh") -> tuple[bool, str]:
    ok, detail = send_ntfy("✅ ShortForge is connected. Progress updates arrive here.",
                           topic=topic, server=server)
    return (ok, "Sent — check the ntfy app (or the topic page in your browser)."
            if ok else f"Failed: {detail}")


def send(text: str, *, silent: bool = False) -> tuple[bool, str]:
    """Send a Telegram message. Returns (ok, detail); never raises."""
    token, chat = _creds()
    if not token or not chat:
        return False, "Telegram not configured (Settings → Telegram)"
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text[:4000],
            "parse_mode": "HTML", "disable_notification": "true" if silent else "false",
        }).encode()
        req = urllib.request.Request(_API.format(token=token, method="sendMessage"), data=data)
        from .netdiag import build_opener
        with build_opener().open(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        if body.get("ok"):
            return True, "sent"
        return False, str(body.get("description") or body)[:200]
    except Exception as e:  # noqa: BLE001 - a failed notify must never break a run
        return False, str(e)[:200]


def notify(text: str) -> None:
    """Fire-and-forget notification over every configured channel.

    Telegram first (richer), ntfy as a fallback that still works where Telegram
    is IP-blocked. A failure on either never interrupts a render.
    """
    token, chat = _creds()
    delivered = False
    if token and chat:
        ok, detail = send(text)
        delivered = delivered or ok
        if not ok:
            log.warning("telegram notify failed: %s", detail)
    if _ntfy_topic():
        ok, detail = send_ntfy(text)
        delivered = delivered or ok
        if not ok:
            log.warning("ntfy notify failed: %s", detail)
    return None


def reachable() -> tuple[bool, str]:
    """Can this machine reach Telegram at all? Distinguishes a network block from
    a bad token — they produce very different fixes."""
    try:
        urllib.request.urlopen("https://api.telegram.org", timeout=12)
        return True, "api.telegram.org is reachable"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


NETWORK_HELP = (
    "This PC cannot reach api.telegram.org — the request timed out before Telegram "
    "answered, so the token was never even checked.\n\n"
    "This is a network block, not a settings problem. Common causes:\n"
    "• Your ISP or country blocks Telegram (very common) — connect a VPN and test again.\n"
    "• A firewall/antivirus is blocking Python's outbound HTTPS.\n"
    "• You're behind a proxy that Python isn't configured to use.\n\n"
    "Quick check: open https://api.telegram.org in your browser. If that also fails "
    "or needs a VPN, Telegram is blocked on this connection — everything else in "
    "ShortForge keeps working, you just won't get phone notifications until it's reachable."
)


def test_telegram(token: str, chat_id: str) -> tuple[bool, str]:
    """Validate credentials from the Settings screen without saving them first."""
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": "✅ ShortForge is connected. You'll get progress updates here.",
        }).encode()
        req = urllib.request.Request(_API.format(token=token, method="sendMessage"), data=data)
        from .netdiag import build_opener
        with build_opener().open(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        if body.get("ok"):
            return True, "Message sent — check your Telegram."
        return False, f"Telegram rejected it: {str(body.get('description') or body)[:200]}"
    except Exception as e:  # noqa: BLE001
        text = str(e).lower()
        if "timed out" in text or "timeout" in text or "urlopen error" in text:
            ok, _ = reachable()
            if not ok:
                return False, NETWORK_HELP
        return False, f"{type(e).__name__}: {str(e)[:250]}"


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
