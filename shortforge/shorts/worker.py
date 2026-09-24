"""Working through a Shorts run.

A run is two kinds of step, taken one at a time from ``run.json``:

1. **List** a queued channel: fetch its newest Shorts, drop every id already
   in the history (or already on disk, or already in this run) and add the
   first N — N being that channel's own count.
2. **Download** the next pending Short.

The state is re-read before every step and written after it, so:

* closing the window, a crash or a power cut loses nothing — the next start
  puts an interrupted Short back to pending, and yt-dlp resumes its ``.part``;
* the screen turning off doesn't matter — the machine is kept awake while a run
  is going (the display may still sleep);
* "Stop after this one" and "Resume" from the dashboard or the phone take effect
  at the next step boundary;
* a Short is never downloaded twice, because the no-repeat check is made right
  before each download, not just when the run was planned.

A run of bot-wall failures pauses the run instead of failing every remaining
Short in turn — the fix (cookies) is the same for all of them.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from ..config import Config
from ..utils import ShortForgeError, log
from . import store
from . import youtube as YT

PROGRESS_FILE = "progress.json"


def _bot_wall(msg: str) -> bool:
    low = (msg or "").lower()
    return "not a bot" in low or "requiring authentication" in low


def _write_progress(dd: str, data: dict) -> None:
    from ..utils import write_json_atomic
    import os
    try:
        write_json_atomic(os.path.join(dd, PROGRESS_FILE), data)
    except OSError:
        pass


def read_progress(dd: str) -> dict:
    import json
    import os
    try:
        with open(os.path.join(dd, PROGRESS_FILE), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _progress_hook_factory(dd: str, item: dict):
    last = [0.0]

    def hook(d: dict) -> None:
        now = time.time()
        if d.get("status") == "downloading" and now - last[0] < 1.0:
            return                         # at most once a second
        last[0] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        _write_progress(dd, {
            "id": item["id"], "title": item.get("title") or "",
            "channel": item.get("channel_name") or item.get("channel_key") or "",
            "phase": d.get("status"), "bytes": done, "total": total,
            "fraction": (done / total) if total else None,
            "speed": d.get("speed"), "eta": d.get("eta"), "at": now,
        })
    return hook


def drain(cfg: Config, *, should_stop: Callable[[], bool] | None = None,
          announce: Callable[[str], None] | None = None,
          dd: str | None = None) -> dict:
    """Work the run until it is finished, paused or ``should_stop()``.

    Returns a summary. ``{"locked": True}`` means another downloader already
    has the run — never two at once, or the same Short would be fetched twice.
    """
    from .. import notify as N
    dd = dd or store.data_dir(cfg)
    announce = announce or N.notify
    if not store.acquire_lock(dd):
        return {"locked": True}

    stop_beat = threading.Event()

    def _beat():
        while not stop_beat.wait(15):
            store.touch_lock(dd)
    threading.Thread(target=_beat, daemon=True, name="shorts-lock-heartbeat").start()
    try:
        with N.KeepAwake("ShortForge is downloading Shorts"):
            return _drain(cfg, dd, should_stop or (lambda: False), announce)
    finally:
        stop_beat.set()
        store.release_lock(dd)
        _write_progress(dd, {})


def _drain(cfg: Config, dd: str, should_stop, announce) -> dict:
    st = store.settings(dd)
    root = YT.output_root(cfg, st)
    resumed = store.requeue_interrupted(dd)
    run = store.load_run(dd)
    _clear_abandoned_staging(dd, run)
    summary = {"downloaded": 0, "failed": 0, "skipped": 0, "bytes": 0,
               "paused": False, "finished": False}
    if store.is_paused(run) or not store.has_work(run):
        summary["finished"] = not store.has_work(run)
        return summary

    c = store.counts(run)
    n_ch = len(run.get("channels_to_list", []))
    announce("📥 <b>Shorts download started</b>\n"
             + (f"{n_ch} channel(s) to check" if n_ch else "")
             + (" · " if n_ch and c[store.PENDING] else "")
             + (f"{c[store.PENDING]} Short(s) queued" if c[store.PENDING] else "")
             + (f"\n↩️ Resumed {resumed} interrupted download(s)." if resumed else "")
             + f"\n📁 {root}")

    known = store.known_ids(dd, root)
    bot_streak = 0
    t_start = time.time()

    while not should_stop():
        run = store.load_run(dd)
        if store.is_paused(run):
            summary["paused"] = True
            break

        # ---- 1. list the next queued channel ------------------------------ #
        if run.get("channels_to_list"):
            key = run["channels_to_list"][0]
            chans = {c["key"]: c for c in store.load_channels(dd)["channels"]}
            ch = chans.get(key)
            if ch is None:              # removed while queued
                store.mutate_run(dd, lambda r: r["channels_to_list"].remove(key)
                                 if key in r["channels_to_list"] else None)
                continue
            _write_progress(dd, {"phase": "listing", "channel": ch.get("name") or key,
                                 "at": time.time()})
            in_plan = {i["id"] for i in run["items"]}
            note = ""
            new: list[dict] = []
            try:
                meta, new, exhausted = YT.list_new_shorts(ch["url"], cfg, int(ch["count"]),
                                                          known | in_plan)
                if meta.get("name") and not ch.get("name"):
                    store.update_channel(dd, key, name=meta["name"])
                    ch["name"] = meta["name"]
                if exhausted:
                    note = (f"only {len(new)} new Short(s) left on this channel "
                            f"(asked for {ch['count']}) — everything else is already "
                            f"downloaded")
                bot_streak = 0
            except ShortForgeError as e:
                note = f"could not list: {str(e)[:200]}"
                log.warning("shorts: %s: %s", key, note)
                if _bot_wall(str(e)):
                    bot_streak += 1

            name = ch.get("name") or key

            def _add(r, key=key, new=new, name=name, note=note):
                if key in r["channels_to_list"]:
                    r["channels_to_list"].remove(key)
                ids = {i["id"] for i in r["items"]}
                for e in new:
                    if e["id"] in ids:
                        continue
                    r["items"].append({
                        "id": e["id"], "url": f"https://www.youtube.com/shorts/{e['id']}",
                        "channel_key": key, "channel_name": name, "source": "channel",
                        "title": e.get("title") or "", "status": store.PENDING,
                        "error": None, "attempts": 0})
                if note:
                    r.setdefault("notes", {})[key] = note
            store.mutate_run(dd, _add)
            if note:
                announce(f"ℹ️ <b>{name}</b>: {note}")
            if bot_streak >= int(st.get("stop_on_bot_wall", 3) or 3):
                return _pause_for_bot_wall(dd, announce, summary)
            continue

        # ---- 2. download the next pending Short --------------------------- #
        item = next((i for i in run["items"] if i.get("status") == store.PENDING), None)
        if item is None:
            summary["finished"] = True
            break
        if item["id"] in known and not item.get("replace"):
            _mark(dd, item["id"], store.SKIPPED, error="already downloaded")
            summary["skipped"] += 1
            continue
        _mark(dd, item["id"], store.DOWNLOADING, bump=True)
        _write_progress(dd, {"phase": "starting", "id": item["id"],
                             "title": item.get("title") or "",
                             "channel": item.get("channel_name") or "", "at": time.time()})
        try:
            rec = YT.download_short(item, cfg, st, _progress_hook_factory(dd, item))
            store.record_download(dd, rec)
            known.add(item["id"])
            _mark(dd, item["id"], store.DONE, file=rec["file"], title=rec["title"])
            summary["downloaded"] += 1
            summary["bytes"] += int(rec.get("filesize") or 0)
            bot_streak = 0
        except ShortForgeError as e:
            _mark(dd, item["id"], store.FAILED, error=str(e)[:400])
            summary["failed"] += 1
            log.warning("shorts: %s failed: %s", item["id"], str(e)[:300])
            if _bot_wall(str(e)):
                bot_streak += 1
                if bot_streak >= int(st.get("stop_on_bot_wall", 3) or 3):
                    return _pause_for_bot_wall(dd, announce, summary)
        except Exception as e:  # noqa: BLE001 - one bad Short must not end the run
            _mark(dd, item["id"], store.FAILED, error=f"{type(e).__name__}: {str(e)[:300]}")
            summary["failed"] += 1
            log.exception("shorts: unexpected failure on %s", item["id"])

        _announce_channel_if_finished(dd, item, announce)
        delay = float(st.get("delay_seconds", 2) or 0)
        if delay > 0 and not should_stop():
            time.sleep(min(delay, 30.0))

    run = store.load_run(dd)
    if summary["finished"]:
        c = store.counts(run)
        mins = (time.time() - t_start) / 60.0
        announce("🏁 <b>Shorts download finished</b>\n"
                 f"✅ {c[store.DONE]} downloaded · ✗ {c[store.FAILED]} failed · "
                 f"⏭ {c[store.SKIPPED]} already had\n"
                 f"💾 {summary['bytes'] / 1e9:.2f} GB this session · {mins:.0f} min\n"
                 f"📁 {root}")
    elif summary["paused"]:
        c = store.counts(run)
        announce(f"⏸ <b>Shorts paused</b> — {c[store.DONE]} downloaded so far, "
                 f"{c[store.PENDING] + len(run.get('channels_to_list', []))} step(s) left. "
                 "Press Resume (or reply “shorts start”) to carry on.")
    return summary


def _clear_abandoned_staging(dd: str, run: dict) -> None:
    """Drop staging folders of Shorts no longer queued (cancelled, or a run that
    was cleared). A queued one keeps its folder so its partial file resumes."""
    import os
    import shutil
    incoming = os.path.join(dd, "incoming")
    if not os.path.isdir(incoming):
        return
    keep = {i["id"] for i in run.get("items", [])
            if i.get("status") in (store.PENDING, store.DOWNLOADING)}
    for name in os.listdir(incoming):
        if name not in keep:
            shutil.rmtree(os.path.join(incoming, name), ignore_errors=True)


def _pause_for_bot_wall(dd: str, announce, summary: dict) -> dict:
    store.set_paused(dd, True)
    # The Shorts that hit the wall go back in line: the fix (cookies) cures them
    # too, so Resume must retry them rather than leave them marked failed.
    store.retry_failed(dd, only_bot_wall=True)
    summary["paused"] = True
    announce("⛔ <b>Shorts paused — YouTube is asking to sign in</b>\n"
             "Several downloads in a row hit YouTube's “confirm you're not a bot” wall, "
             "so I stopped instead of failing every remaining Short.\n"
             "Fix: Settings → 📺 YouTube authentication → pick Firefox (signed in to "
             "YouTube) → Test. Then press Resume in the Shorts tab.")
    return summary


def _mark(dd: str, vid: str, status: str, *, error: str | None = None,
          bump: bool = False, **extra) -> None:
    def fn(r):
        for it in r["items"]:
            if it["id"] == vid:
                it["status"] = status
                it["error"] = error
                if bump:
                    it["attempts"] = int(it.get("attempts", 0)) + 1
                    it["started"] = time.time()
                if status in (store.DONE, store.FAILED, store.SKIPPED):
                    it["finished"] = time.time()
                it.update({k: v for k, v in extra.items() if v})
                return
    store.mutate_run(dd, fn)


def _announce_channel_if_finished(dd: str, item: dict, announce) -> None:
    key = item.get("channel_key")
    if not key:
        return
    run = store.load_run(dd)
    mine = [i for i in run["items"] if i.get("channel_key") == key]
    if any(i["status"] in (store.PENDING, store.DOWNLOADING) for i in mine):
        return
    done = sum(1 for i in mine if i["status"] == store.DONE)
    failed = sum(1 for i in mine if i["status"] == store.FAILED)
    if not done and not failed:
        return
    announce(f"✅ <b>{item.get('channel_name') or key}</b> — {done} Short(s) downloaded"
             + (f", {failed} failed" if failed else ""))
