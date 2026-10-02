"""Tidy a Shorts folder written by the first version of the downloader.

That version wrote straight into the output folder, so next to every video sat
a ``.txt``, a ``.json`` and a ``.jpg`` — and, whenever YouTube cut a download
off, an 80–90 MB ``.part`` or a video-only ``.f137.mp4`` that nothing ever
removed. Files were also named ``Title [id].mp4``, which is meaningless when a
creator gives every Short the same title.

``plan()`` looks and changes nothing; ``apply()`` carries out a plan the
operator has seen. Only files this program named are touched — every one of
them carries the ``[video id]`` in its name — so anything else in the folder is
left exactly as it is.
"""

from __future__ import annotations

import os
import re
import shutil

from ..config import Config
from ..utils import log
from . import store as S
from . import youtube as YT

_ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
# yt-dlp's unfinished pieces: partial downloads, resume markers, per-fragment
# files, pre-merge temp files and the separate video/audio streams (.f137.mp4).
_LEFTOVER = re.compile(r"(\.part$|\.ytdl$|\.part-frag\d+|\.temp\.[a-z0-9]+$|"
                       r"\.f\d+(?:-\d+)?\.[a-z0-9]+$)", re.IGNORECASE)
_VIDEO = (".mp4", ".mkv", ".webm", ".m4v", ".mov")
_THUMB = (".jpg", ".jpeg", ".webp", ".png")


def plan(dd: str, cfg: Config) -> dict:
    st = S.settings(dd)
    root = YT.output_root(cfg, st)
    hist = S.load_history(dd)["items"]
    deletes: list[tuple[str, str]] = []
    renames: list[dict] = []

    for folder, _dirs, files in os.walk(root):
        old_videos: list[tuple[str, str]] = []            # (path, id)
        for name in sorted(files):
            m = _ID_IN_NAME.search(name)
            if not m:
                continue                                   # not ours — never touched
            path = os.path.join(folder, name)
            low = name.lower()
            ext = os.path.splitext(low)[1]
            if _LEFTOVER.search(low):
                deletes.append((path, "unfinished download piece"))
            elif ext in _VIDEO:
                old_videos.append((path, m.group(1)))
            elif ext == ".txt" and not st.get("sidecar_txt"):
                deletes.append((path, "title/description file — now inside the video "
                                      "and in History"))
            elif ext == ".json" and not st.get("sidecar_json"):
                deletes.append((path, "metadata file — kept in History"))
            elif ext in _THUMB and not st.get("save_thumbnail"):
                deletes.append((path, "thumbnail"))

        # Number the old-style videos in the order they were downloaded, after
        # anything already numbered in this folder.
        def when(pv):
            rec = hist.get(pv[1]) or {}
            try:
                return rec.get("downloaded_at") or os.path.getmtime(pv[0])
            except OSError:
                return 0
        n = S.next_number(dd, folder)
        for path, vid in sorted(old_videos, key=when):
            rec = hist.get(vid) or {}
            title = rec.get("title") or _ID_IN_NAME.sub("", os.path.splitext(
                os.path.basename(path))[0]).strip()
            ext = os.path.splitext(path)[1]
            base = YT.numbered_name(n, title, st)
            renames.append({"id": vid, "from": path,
                            "to": os.path.join(folder, base + ext), "number": n,
                            "folder": folder})
            n += 1

    incoming = os.path.join(dd, "incoming")
    run = S.load_run(dd)
    live = {i["id"] for i in run.get("items", [])
            if i.get("status") in (S.PENDING, S.DOWNLOADING)}
    stale_staging = []
    if os.path.isdir(incoming) and not S.lock_is_live(dd):
        stale_staging = [os.path.join(incoming, d) for d in os.listdir(incoming)
                         if d not in live]

    freed = 0
    for p, _why in deletes:
        try:
            freed += os.path.getsize(p)
        except OSError:
            pass
    return {"root": root, "deletes": deletes, "renames": renames,
            "stale_staging": stale_staging, "bytes": freed}


def describe(p: dict) -> str:
    parts = []
    if p["deletes"]:
        parts.append(f"remove {len(p['deletes'])} extra file(s) "
                     f"({p['bytes'] / 1e6:.0f} MB)")
    if p["renames"]:
        parts.append(f"number {len(p['renames'])} video(s) in download order")
    if p["stale_staging"]:
        parts.append(f"clear {len(p['stale_staging'])} abandoned download(s)")
    return ("Will " + ", ".join(parts) + ".") if parts else "Nothing to tidy — already clean."


def apply(dd: str, p: dict) -> dict:
    removed = renamed = 0
    for path, _why in p["deletes"]:
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            log.warning("tidy: could not remove %s (%s)", path, e)
    moved: dict[str, dict] = {}
    for r in p["renames"]:
        if os.path.exists(r["to"]) or not os.path.exists(r["from"]):
            continue                                      # never overwrite anything
        try:
            os.rename(r["from"], r["to"])
        except OSError as e:
            log.warning("tidy: could not rename %s (%s)", r["from"], e)
            continue
        renamed += 1
        S.commit_number(dd, r["folder"], r["number"])
        moved[r["id"]] = r
        # A sidecar the operator chose to keep follows its video's new name.
        old_stem = os.path.splitext(r["from"])[0]
        new_stem = os.path.splitext(r["to"])[0]
        for ext in (".txt", ".json") + _THUMB:
            if os.path.exists(old_stem + ext) and not os.path.exists(new_stem + ext):
                try:
                    os.rename(old_stem + ext, new_stem + ext)
                except OSError:
                    pass
    if moved:
        def fn(d):
            for vid, r in moved.items():
                rec = d["items"].get(vid)
                if rec:
                    rec["file"] = os.path.abspath(r["to"])
                    rec["number"] = r["number"]
        S._mutate(dd, S.HISTORY_FILE, {"version": 1, "items": {}}, fn)
    for d in p["stale_staging"]:
        shutil.rmtree(d, ignore_errors=True)
    return {"removed": removed, "renamed": renamed, "staging": len(p["stale_staging"])}
