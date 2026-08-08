"""Control ShortForge from your phone over ntfy.sh — the only remote-control
surface (Telegram was removed; ntfy needs no account/bot setup and has a free
phone app, so there was no reason to keep two).

SECURITY — read this before using it:
  ntfy topics are public by default: anyone who guesses the topic name can PUBLISH
  to it, and publishing is how commands arrive. So the command topic must be a
  long unguessable string, and it should NOT be the same topic you use for
  notifications (that one gets shared/screenshotted more often). An optional
  access token is supported for real protection. The command surface is the same
  narrow one as Telegram's: links + whitelisted key=value settings, or a fixed
  set of /commands. No shell, no filesystem, no arbitrary config.
"""

from __future__ import annotations

import json
import time
import urllib.request

from .remote_control import handle_text, log_exchange
from .utils import log


def _creds() -> tuple[str | None, str, str | None]:
    """(command_topic, server, token) from the provider store."""
    try:
        from .providers.store import load_store
        nt = load_store().get("ntfy", {}) or {}
        topic = nt.get("command_topic") or None
        server = (nt.get("server") or "https://ntfy.sh").rstrip("/")
        return topic, server, (nt.get("token") or None)
    except Exception:  # noqa: BLE001
        return None, "https://ntfy.sh", None


def configured() -> bool:
    return bool(_creds()[0])


def poll_once(topic: str, server: str, since: str | int, token: str | None = None,
              work_dir: str = ".shortforge", on_result=None) -> tuple[str | int, int]:
    """Fetch messages published since ``since`` and act on them.

    Returns (new_since, handled_count). Never raises — a network blip just means
    we try again on the next tick. ``on_result(ok, detail)`` (optional) reports
    whether the server was reachable, so an outage isn't silently swallowed.
    """
    from .netdiag import build_opener
    from .notify import send_ntfy

    url = f"{server}/{topic}/json?poll=1&since={since}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with build_opener().open(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
        if on_result:
            on_result(True, "")
    except Exception as e:  # noqa: BLE001
        log.debug("ntfy poll error: %s", e)
        if on_result:
            on_result(False, f"{type(e).__name__}: {str(e)[:150]}")
        time.sleep(5)
        return since, 0

    handled = 0
    newest = since
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("event") != "message":
            continue
        newest = max(int(msg.get("time", 0)) + 1, int(newest) if str(newest).isdigit() else 0)
        text = (msg.get("message") or "").strip()
        if not text:
            continue
        try:
            reply = handle_text(text, work_dir)      # shared with the dashboard's Chat screen
        except Exception as e:  # noqa: BLE001
            log.error("ntfy handler error: %s", e)
            reply = "Something went wrong handling that. Send /help."
        handled += 1
        send_ntfy(reply)                             # answer on the notification topic
        log_exchange(work_dir, "ntfy", text, reply)   # so the dashboard shows this exchange too
    return newest, handled


def listen(work_dir: str = ".shortforge", stop=None, interval: int = 5) -> None:
    """Poll the command topic until ``stop()`` returns True."""
    from . import lifecycle
    from .notify import OutageWatch

    topic, server, token = _creds()
    if not topic:
        log.warning("ntfy commands not configured (Settings → Notifications → command topic)")
        return
    since = int(time.time())          # ignore anything sent before we started
    # A dropped connection here is the operator's own internet: worth reporting,
    # but only after a few consecutive misses (one blip is normal). The alert
    # itself goes over ntfy too, so it can only land for certain on recovery —
    # `lifecycle.note_ntfy_status` also records it locally (runstate.json) so
    # the dashboard can show "disconnected" even while no push can get through.
    watch = OutageWatch("ntfy", threshold=4)

    def _on_result(ok: bool, detail: str) -> None:
        watch.record(ok, detail)
        lifecycle.note_ntfy_status(work_dir, ok, detail)

    log.info("ntfy: listening for commands on '%s'", topic[:6] + "…")
    while not (stop and stop()):
        since, n = poll_once(topic, server, since, token, work_dir,
                             on_result=_on_result)
        if n:
            log.info("ntfy: handled %d command(s)", n)
        time.sleep(interval)
