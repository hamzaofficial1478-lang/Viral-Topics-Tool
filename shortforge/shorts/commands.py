"""Phone commands for the Shorts downloader (ntfy / the command topic).

Always prefixed with "shorts" so they can never collide with the clip queue's
own words — a bare "start" keeps meaning "start the clip queue", exactly as
before. Channels themselves are managed in the dashboard, where the rights
confirmation lives; the phone can start, pause, retry and check.
"""

from __future__ import annotations

import re

from . import store

HELP = (
    "<b>YouTube Shorts downloader</b>\n"
    "<b>shorts</b> — what it's doing\n"
    "<b>shorts start</b> — download the next batch from every channel that is switched "
    "on (or carry on a paused run)\n"
    "<b>shorts pause</b> — stop after the Short being downloaded\n"
    "<b>shorts retry</b> — try the failed ones again\n"
    "<b>shorts cancel</b> — drop everything still queued\n"
    "<b>shorts history</b> — the last 10 downloads"
)

# "shorts" exactly (not "short"): "3 shorts 1:30" is clip-queue phrasing.
_PREFIX = re.compile(r"^\s*/?shorts\b\s*(.*)$", re.IGNORECASE | re.DOTALL)


def matches(text: str) -> bool:
    return bool(_PREFIX.match(text or ""))


def status_line(dd: str | None = None) -> str:
    dd = dd or store.data_dir()
    run = store.load_run(dd)
    c = store.counts(run)
    to_list = len(run.get("channels_to_list", []))
    if store.lock_is_live(dd):
        state = "▶ downloading"
    elif store.has_work(run):
        state = "⏸ paused" if store.is_paused(run) else "⏳ waiting for a worker"
    else:
        state = "idle"
    total = len(store.load_history(dd)["items"])
    line = (f"📥 <b>Shorts</b> — {state} · ✅ {c[store.DONE]} this run · "
            f"⏳ {c[store.PENDING] + to_list} to go · ✗ {c[store.FAILED]} failed · "
            f"{total} downloaded in total")
    return line


def handle(text: str, dd: str | None = None) -> str:
    dd = dd or store.data_dir()
    m = _PREFIX.match(text or "")
    arg = (m.group(1) if m else "").strip().lower()
    word = arg.split()[0] if arg else ""

    if word in ("", "status", "?"):
        return status_line(dd)
    if word in ("help", "/help"):
        return HELP
    if word in ("start", "go", "run", "resume", "continue", "download"):
        run = store.load_run(dd)
        if store.has_work(run):
            store.set_paused(dd, False)
            return "▶️ <b>Shorts resumed.</b> " + status_line(dd)
        chans = [c for c in store.load_channels(dd)["channels"] if c.get("enabled", True)]
        if not chans:
            return ("No channels are switched on. Add channels in the dashboard → "
                    "📥 YT Shorts → Channels.")
        store.start_run(dd, [c["key"] for c in chans], [])
        n = sum(int(c.get("count", 0)) for c in chans)
        return (f"▶️ <b>Shorts started</b> — up to {n} new Short(s) from "
                f"{len(chans)} channel(s). I'll message you as each channel finishes.")
    if word in ("pause", "stop", "hold", "wait"):
        store.set_paused(dd, True)
        return "⏸ <b>Shorts paused</b> — the one downloading now finishes first."
    if word in ("retry", "again"):
        n = store.retry_failed(dd)
        if n:
            store.set_paused(dd, False)
        return (f"🔁 {n} failed Short(s) back in line." if n
                else "Nothing failed in the current run.")
    if word in ("cancel", "clear", "drop"):
        return f"🛑 Dropped {store.cancel_pending(dd)} queued step(s)."
    if word in ("history", "list", "last"):
        items = sorted(store.load_history(dd)["items"].values(),
                       key=lambda r: r.get("downloaded_at") or 0, reverse=True)[:10]
        if not items:
            return "No Shorts downloaded yet."
        return "<b>Last downloads</b>\n" + "\n".join(
            f"• {(r.get('channel_name') or '-')[:18]} — {r['title'][:50]}"
            f" ({r.get('height') or '?'}p)" for r in items)
    return "I didn't understand that.\n\n" + HELP
