"""Everything the Shorts downloader keeps on disk.

Three files in one folder (``shorts.data_dir``, default ``shorts_data/`` next to
the program — deliberately NOT inside ``.shortforge/``, which cache-clearing and
pruning are allowed to empty):

* ``channels.json`` — the saved channels, each with its own count and on/off
  switch, plus the Shorts settings. Only ever changed by the operator.
* ``history.json``  — every Short downloaded, keyed by video id. This is the
  no-repeat archive: an id in here is never downloaded again.
* ``run.json``      — the current run: which channels still need listing and
  which Shorts are still to download. It is what lets a run carry on after the
  screen turns off, the window is closed or the power goes.

Why a file of its own rather than the settings store: the Settings screen
rewrites that whole file from the copy it loaded, so a Settings save made in
another browser tab would silently roll back a channel added a minute earlier.
The operator was explicit that saved channels and counts must never be lost.

Every write is atomic (temp file + rename) and keeps the previous good version
as ``<file>.bak``; a file that fails to parse is read from its backup instead
of being treated as empty — an empty history would mean re-downloading
everything.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid

from ..utils import log, write_json_atomic

CHANNELS_FILE = "channels.json"
HISTORY_FILE = "history.json"
RUN_FILE = "run.json"
LOCK_FILE = "worker.lock"
LOG_FILE = "worker.log"

# Per-run statuses of one Short in the plan.
PENDING, DOWNLOADING, DONE, SKIPPED, FAILED, CANCELLED = (
    "pending", "downloading", "done", "skipped", "failed", "cancelled")

DEFAULT_SETTINGS: dict = {
    "default_count": 10,          # Shorts per run for a newly added channel
    "output_dir": "",             # "" -> <paths.output_dir>/shorts
    "folder_per_channel": True,
    # Quality: "best" never caps anything. A number caps the height (e.g. 1080)
    # for someone short on disk space — the operator's default is no compromise.
    "max_height": "best",
    # "best" takes the highest resolution/fps whatever the codec (often VP9 or
    # AV1). "compatible" still takes the highest resolution, but prefers H.264
    # among formats of that same resolution, so older players cope.
    "codec": "best",
    "container": "mp4",           # mp4 | mkv
    # The operator wants the output folder to hold videos and nothing else, so
    # the title, description and hashtags are written INTO each file and kept in
    # the history — the separate files are opt-in. (The thumbnail setting was
    # renamed from write_thumbnail so an old saved "on" doesn't bring it back.)
    "embed_metadata": True,       # title/description/hashtags inside the video
    "save_thumbnail": False,      # <n> - <title>.jpg next to the video
    "sidecar_txt": False,         # <n> - <title>.txt  (title/description/hashtags)
    "sidecar_json": False,        # <n> - <title>.json (everything known)
    "name_style": "number_title", # "1 - Title.mp4" | "number" → "1.mp4"
    "ai_fill_missing": False,     # AI writes a description/hashtags when none exist
    # Quality is not traded away by default: if YouTube won't deliver the best
    # picture it offers, that Short fails (and can be retried) instead of being
    # saved at a lower resolution. On = keep the best that could be downloaded.
    "allow_lower_quality": False,
    "delay_seconds": 2,           # pause between downloads — fewer bot walls
    "stop_on_bot_wall": 3,        # pause the run after N bot walls in a row
}

_VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
_ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.[A-Za-z0-9]{2,4}$")


# --- paths + safe JSON ------------------------------------------------------- #

def data_dir(cfg=None) -> str:
    if cfg is None:
        from ..config import Config
        cfg = Config.load()
    return str(cfg.get("shorts.data_dir", "shorts_data") or "shorts_data")


def _path(dd: str, name: str) -> str:
    return os.path.join(dd, name)


def _load(path: str, default: dict) -> dict:
    """Read JSON; fall back to the ``.bak`` if the main file is damaged."""
    for candidate in (path, path + ".bak"):
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            if candidate != path:
                log.warning("shorts: %s was unreadable — using its backup",
                            os.path.basename(path))
            return data
        except (OSError, ValueError, json.JSONDecodeError) as e:
            log.warning("shorts: could not read %s (%s)", candidate, e)
    return json.loads(json.dumps(default))          # deep copy


def _save(path: str, data: dict) -> None:
    """Atomic write, keeping the previous good file as ``.bak``."""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)                     # only back up a file that parses
            shutil.copy2(path, path + ".bak")
        except (OSError, ValueError):
            pass
    write_json_atomic(path, data)


def _mutate(dd: str, name: str, default: dict, fn):
    """Load → ``fn(data)`` → save, serialised against other processes."""
    from ..queue import queue_mutex
    os.makedirs(dd, exist_ok=True)
    with queue_mutex(dd):
        data = _load(_path(dd, name), default)
        result = fn(data)
        _save(_path(dd, name), data)
    return data, result


# --- parsing what the operator pastes ---------------------------------------- #

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VIDEO_URL = re.compile(
    r"(?:youtube\.com/(?:shorts/|live/|embed/|watch\?(?:[^#\s]*&)?v=)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})")
_HANDLE = re.compile(r"^@[^\s/?#]{2,100}$")
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_CHANNEL_URL = re.compile(
    r"youtube\.com/(@[^\s/?#]+|channel/UC[A-Za-z0-9_-]{22}|c/[^\s/?#]+|user/[^\s/?#]+)",
    re.IGNORECASE)


def video_id_from_link(text: str) -> str | None:
    """The 11-character id from any Short/video link, or None."""
    t = (text or "").strip()
    m = _VIDEO_URL.search(t)
    if m:
        return m.group(1)
    return t if _VIDEO_ID.match(t) and not t.startswith("UC") else None


def parse_channel(text: str) -> tuple[str, str]:
    """``(key, channel_url)`` for a pasted channel reference.

    Accepts ``@handle``, a channel URL (``/@handle``, ``/channel/UC…``, ``/c/…``,
    ``/user/…`` — any trailing ``/shorts``, ``/videos`` or query is dropped), or
    a bare channel id (``UC…``). Raises ValueError with a plain reason otherwise.
    """
    t = (text or "").strip().strip("<>").strip()
    if not t:
        raise ValueError("empty line")
    if _VIDEO_URL.search(t):
        raise ValueError("that is a link to one video, not a channel — paste it "
                         "under “Paste links” instead")
    if _HANDLE.match(t):
        path = t
    elif _CHANNEL_ID.match(t):
        path = f"channel/{t}"
    else:
        m = _CHANNEL_URL.search(t)
        if not m:
            raise ValueError("not a YouTube channel — use @handle, a channel link, "
                             "or a channel id starting with UC")
        path = m.group(1)
    if path.startswith("@") or path.lower().startswith(("c/", "user/")):
        key = path.lower()
    else:                                  # channel ids are case-sensitive
        key = "channel/" + path.split("/", 1)[1]
    return key, f"https://www.youtube.com/{path}"


def shorts_tab_url(channel_url: str) -> str:
    return channel_url.rstrip("/") + "/shorts"


# --- channels + settings ----------------------------------------------------- #

_CHANNELS_DEFAULT = {"version": 1, "settings": {}, "channels": []}


def load_channels(dd: str) -> dict:
    data = _load(_path(dd, CHANNELS_FILE), _CHANNELS_DEFAULT)
    data.setdefault("channels", [])
    data.setdefault("settings", {})
    return data


def settings(dd: str) -> dict:
    """Saved Shorts settings with defaults filled in for anything unset."""
    return {**DEFAULT_SETTINGS, **(load_channels(dd).get("settings") or {})}


def update_settings(dd: str, **fields) -> dict:
    def fn(d):
        s = d.setdefault("settings", {})
        for k, v in fields.items():
            if k in DEFAULT_SETTINGS:
                s[k] = v
    return {**DEFAULT_SETTINGS, **_mutate(dd, CHANNELS_FILE, _CHANNELS_DEFAULT, fn)[0]["settings"]}


def add_channels(dd: str, text: str, count: int, *,
                 rights_confirmed: bool) -> tuple[list[dict], list[str]]:
    """Add every channel in ``text`` (one per line, or comma/space separated).

    Returns ``(added, problems)``; a channel already saved is reported, not
    duplicated, and keeps its own count.
    """
    if not rights_confirmed:
        return [], ["Tick the box confirming these are your channels, or that you "
                    "have the rights to reuse their Shorts."]
    count = max(1, min(int(count or 1), 500))
    items = [p for line in (text or "").splitlines()
             for p in re.split(r"[,\s]+", line.strip()) if p]
    parsed: list[tuple[str, str]] = []
    problems: list[str] = []
    for item in items:
        try:
            parsed.append(parse_channel(item))
        except ValueError as e:
            problems.append(f"{item[:60]}: {e}")

    def fn(d):
        have = {c["key"] for c in d["channels"]}
        added = []
        for key, url in parsed:
            if key in have:
                problems.append(f"{key}: already saved — its count was left as it is")
                continue
            ch = {"key": key, "url": url, "name": "", "count": count, "enabled": True,
                  "added": time.time(), "rights_confirmed": True}
            d["channels"].append(ch)
            have.add(key)
            added.append(ch)
        return added

    _data, added = _mutate(dd, CHANNELS_FILE, _CHANNELS_DEFAULT, fn)
    return added, problems


def update_channel(dd: str, key: str, **fields) -> dict | None:
    allowed = {"count", "enabled", "name"}

    def fn(d):
        for c in d["channels"]:
            if c["key"] == key:
                for k, v in fields.items():
                    if k in allowed:
                        c[k] = max(1, min(int(v), 500)) if k == "count" else v
                return dict(c)
        return None
    return _mutate(dd, CHANNELS_FILE, _CHANNELS_DEFAULT, fn)[1]


def set_all_enabled(dd: str, enabled: bool) -> None:
    def fn(d):
        for c in d["channels"]:
            c["enabled"] = bool(enabled)
    _mutate(dd, CHANNELS_FILE, _CHANNELS_DEFAULT, fn)


def remove_channel(dd: str, key: str) -> bool:
    def fn(d):
        before = len(d["channels"])
        d["channels"] = [c for c in d["channels"] if c["key"] != key]
        return len(d["channels"]) < before
    return bool(_mutate(dd, CHANNELS_FILE, _CHANNELS_DEFAULT, fn)[1])


# --- history (the no-repeat archive) ----------------------------------------- #

_HISTORY_DEFAULT = {"version": 1, "items": {}}


def load_history(dd: str) -> dict:
    data = _load(_path(dd, HISTORY_FILE), _HISTORY_DEFAULT)
    data.setdefault("items", {})
    return data


def record_download(dd: str, rec: dict) -> None:
    def fn(d):
        d["items"][rec["id"]] = rec
    _mutate(dd, HISTORY_FILE, _HISTORY_DEFAULT, fn)


def forget(dd: str, video_id: str) -> bool:
    """Drop one Short from the history so it may be downloaded again."""
    def fn(d):
        return d["items"].pop(video_id, None) is not None
    return bool(_mutate(dd, HISTORY_FILE, _HISTORY_DEFAULT, fn)[1])


def ids_on_disk(output_root: str) -> set[str]:
    """Video ids of finished files under the output folder (``… [id].mp4``).

    The second line of defence against a repeat download: if history.json were
    ever lost, the files themselves still say what has been fetched. Partial
    ``.part`` files are ignored, so an interrupted download is resumed rather
    than counted as done.
    """
    found: set[str] = set()
    if not output_root or not os.path.isdir(output_root):
        return found
    for _root, _dirs, files in os.walk(output_root):
        for name in files:
            if name.lower().endswith(_VIDEO_EXTS):
                m = _ID_IN_NAME.search(name)
                if m:
                    found.add(m.group(1))
    return found


def known_ids(dd: str, output_root: str | None = None) -> set[str]:
    ids = set(load_history(dd)["items"])
    if output_root:
        ids |= ids_on_disk(output_root)
    return ids


def taken_by_channel(dd: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for rec in load_history(dd)["items"].values():
        k = rec.get("channel_key") or ""
        if k:
            out[k] = out.get(k, 0) + 1
    return out


# --- download-order numbering ----------------------------------------------- #
# "1 - Title.mp4", "2 - …": the number is the order a Short arrived in its
# folder, and it keeps counting across runs — ask for 3, then 3 more, and the
# second batch is 4, 5, 6, never 1, 2, 3 again.

COUNTERS_FILE = "counters.json"
_NUMBERED = re.compile(r"^(\d+)(?:\s*-\s|\.[A-Za-z0-9]{2,4}$)")


def _folder_key(folder: str) -> str:
    return os.path.normcase(os.path.abspath(folder))


def highest_number_in(folder: str) -> int:
    """The largest ``N`` among ``N - ….mp4`` / ``N.mp4`` videos in ``folder``."""
    best = 0
    try:
        for name in os.listdir(folder):
            if name.lower().endswith(_VIDEO_EXTS):
                m = _NUMBERED.match(name)
                if m:
                    best = max(best, int(m.group(1)))
    except OSError:
        pass
    return best


def next_number(dd: str, folder: str) -> int:
    """Next number for ``folder``: past the saved counter AND past anything
    already in the folder. Either alone would do; both means that losing the
    counter file, or dropping extra numbered files in, still never re-uses a
    number. Deleting videos never makes the count go back down."""
    saved = int(_load(_path(dd, COUNTERS_FILE), {"folders": {}})
                .get("folders", {}).get(_folder_key(folder), 0) or 0)
    return max(saved, highest_number_in(folder)) + 1


def commit_number(dd: str, folder: str, number: int) -> None:
    def fn(d):
        f = d.setdefault("folders", {})
        k = _folder_key(folder)
        f[k] = max(int(f.get(k, 0) or 0), int(number))
    _mutate(dd, COUNTERS_FILE, {"folders": {}}, fn)


# --- the current run --------------------------------------------------------- #

_RUN_DEFAULT = {"version": 1, "run_id": "", "created": 0, "paused": False,
                "channels_to_list": [], "items": [], "notes": {}}


def load_run(dd: str) -> dict:
    data = _load(_path(dd, RUN_FILE), _RUN_DEFAULT)
    for k, v in _RUN_DEFAULT.items():
        data.setdefault(k, json.loads(json.dumps(v)))
    return data


def mutate_run(dd: str, fn):
    return _mutate(dd, RUN_FILE, _RUN_DEFAULT, fn)


def counts(run: dict) -> dict:
    c = {s: 0 for s in (PENDING, DOWNLOADING, DONE, SKIPPED, FAILED, CANCELLED)}
    for it in run.get("items", []):
        c[it.get("status", PENDING)] = c.get(it.get("status", PENDING), 0) + 1
    return c


def has_work(run: dict) -> bool:
    return bool(run.get("channels_to_list")) or counts(run)[PENDING] > 0 \
        or counts(run)[DOWNLOADING] > 0


def is_paused(run: dict) -> bool:
    return bool(run.get("paused"))


def set_paused(dd: str, paused: bool) -> None:
    def fn(r):
        r["paused"] = bool(paused)
    mutate_run(dd, fn)


def start_run(dd: str, channel_keys: list[str], links: list[str]) -> dict:
    """Queue channels to be listed and/or links to download, and un-pause.

    Adds to a run already in progress rather than replacing it, so pressing
    Download twice — or pasting links while channels are downloading — never
    loses work. Returns what was queued, for the confirmation message.
    """
    new_links: list[tuple[str, str]] = []
    bad: list[str] = []
    for link in links:
        vid = video_id_from_link(link)
        if vid:
            new_links.append((vid, f"https://www.youtube.com/shorts/{vid}"))
        elif link.strip():
            bad.append(link.strip()[:80])

    def fn(r):
        if not r.get("run_id") or not has_work(r):
            # A finished run's list is replaced; its downloads live on in the
            # history, which is the permanent record.
            r.update({"run_id": uuid.uuid4().hex[:8], "created": time.time(),
                      "items": [], "notes": {}, "channels_to_list": []})
        queued_ch = [k for k in channel_keys if k not in r["channels_to_list"]]
        r["channels_to_list"].extend(queued_ch)
        in_plan = {i["id"] for i in r["items"]}
        queued_links = 0
        for vid, url in new_links:
            if vid in in_plan:
                continue
            r["items"].append({"id": vid, "url": url, "channel_key": "", "channel_name": "",
                               "source": "link", "title": "", "status": PENDING,
                               "error": None, "attempts": 0})
            in_plan.add(vid)
            queued_links += 1
        r["paused"] = False
        return {"channels": len(queued_ch), "links": queued_links, "bad_links": bad}
    return mutate_run(dd, fn)[1]


def queue_redownload(dd: str, records: list[dict]) -> int:
    """Download these Shorts again at best quality, each replacing its earlier
    copy and keeping its number. Returns how many were queued."""
    def fn(r):
        if not r.get("run_id") or not has_work(r):
            r.update({"run_id": uuid.uuid4().hex[:8], "created": time.time(),
                      "items": [], "notes": {}, "channels_to_list": []})
        queued = {i["id"] for i in r["items"] if i.get("status") in (PENDING, DOWNLOADING)}
        n = 0
        for rec in records:
            if rec["id"] in queued:
                continue
            r["items"].append({
                "id": rec["id"], "url": rec.get("url") or
                f"https://www.youtube.com/shorts/{rec['id']}",
                "channel_key": rec.get("channel_key") or "",
                "channel_name": rec.get("channel_name") or "",
                "source": "redownload", "title": rec.get("title") or "",
                "status": PENDING, "error": None, "attempts": 0,
                "replace": {"number": rec.get("number"), "file": rec.get("file")}})
            n += 1
        r["paused"] = False
        return n
    return mutate_run(dd, fn)[1]


def requeue_interrupted(dd: str) -> int:
    """A Short left ``downloading`` by a crash goes back to pending. yt-dlp
    resumes its ``.part`` file, so nothing already fetched is fetched twice."""
    def fn(r):
        n = 0
        for it in r["items"]:
            if it.get("status") == DOWNLOADING:
                it["status"] = PENDING
                n += 1
        return n
    return mutate_run(dd, fn)[1]


def retry_failed(dd: str, *, only_bot_wall: bool = False) -> int:
    """Put failed Shorts back in line. ``only_bot_wall`` limits it to the ones
    YouTube refused for want of sign-in — the ones fixing cookies will cure."""
    def fn(r):
        n = 0
        for it in r["items"]:
            if it.get("status") != FAILED:
                continue
            err = (it.get("error") or "").lower()
            if only_bot_wall and not ("not a bot" in err or "requiring authentication" in err):
                continue
            it["status"] = PENDING
            it["error"] = None
            n += 1
        return n
    return mutate_run(dd, fn)[1]


def cancel_pending(dd: str) -> int:
    def fn(r):
        n = len(r.get("channels_to_list", []))
        r["channels_to_list"] = []
        for it in r["items"]:
            if it.get("status") == PENDING:
                it["status"] = CANCELLED
                n += 1
        return n
    return mutate_run(dd, fn)[1]


def clear_finished(dd: str) -> int:
    """Forget a finished run's list (the history keeps every download)."""
    def fn(r):
        if has_work(r):
            return 0
        n = len(r["items"])
        r.update({"items": [], "notes": {}, "channels_to_list": [], "run_id": ""})
        return n
    return mutate_run(dd, fn)[1]


# --- worker lock (one downloader at a time) ---------------------------------- #

LOCK_STALE_AFTER = 120.0


def lock_is_live(dd: str) -> bool:
    """Is a downloader working right now? (Its lock is touched every ~15s.)"""
    from ..utils import pid_alive
    p = _path(dd, LOCK_FILE)
    try:
        age = time.time() - os.path.getmtime(p)
        with open(p, "r", encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return False
    return age < LOCK_STALE_AFTER and pid > 0 and pid_alive(pid)


def acquire_lock(dd: str) -> bool:
    os.makedirs(dd, exist_ok=True)
    p = _path(dd, LOCK_FILE)
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            if lock_is_live(dd):
                return False
            try:                                  # stale: its holder is gone
                os.remove(p)
            except OSError:
                return False
    return False


def touch_lock(dd: str) -> None:
    try:
        os.utime(_path(dd, LOCK_FILE), None)
    except OSError:
        pass


def release_lock(dd: str) -> None:
    try:
        os.remove(_path(dd, LOCK_FILE))
    except OSError:
        pass
