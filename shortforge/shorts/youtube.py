"""Listing a channel's Shorts, downloading one, and describing it.

Downloads go through ``ingest.download_resilient`` — the same cookie chain,
player-client rotation and format fallback the long-video maker uses — so the
next thing YouTube changes is fixed in one place for both tools.

Quality: the default selector is ``bv*+ba/b`` with yt-dlp's own quality sort
(resolution, then frame rate, then codec efficiency), streams are merged
without re-encoding, and nothing is scaled. What YouTube serves is what lands
on disk.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time

from ..config import Config
from ..utils import ShortForgeError, log
from . import store

_HASHTAG = re.compile(r"#([^\s#.,!?;:()\[\]{}\"'<>|/\\]{2,60})", re.UNICODE)
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "you", "your", "from", "what", "when",
    "how", "why", "are", "was", "were", "will", "have", "has", "had", "not", "but",
    "can", "just", "they", "them", "their", "its", "into", "out", "about", "more",
    "most", "some", "than", "then", "there", "these", "those", "who", "all", "any",
    "our", "his", "her", "she", "him", "get", "got", "one", "day", "new", "video",
    "shorts", "short", "watch", "like", "subscribe",
}


# --- where files go ---------------------------------------------------------- #

def output_root(cfg: Config, st: dict) -> str:
    chosen = str(st.get("output_dir") or "").strip()
    if chosen:
        return os.path.abspath(os.path.expanduser(chosen))
    return os.path.abspath(os.path.join(cfg.get("paths.output_dir", "out"), "shorts"))


def _safe_folder(name: str) -> str:
    """A Windows-safe folder name (no reserved characters, no trailing dot)."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip().rstrip(". ")
    return s[:80] or "channel"


def destination(cfg: Config, st: dict, item: dict) -> str:
    root = output_root(cfg, st)
    if not st.get("folder_per_channel", True):
        return root
    name = item.get("channel_name") or item.get("channel_key") or ""
    return os.path.join(root, _safe_folder(name.lstrip("@") or "Pasted links"))


# --- listing ----------------------------------------------------------------- #

def _flat_list(url: str, cfg: Config, limit: int) -> dict:
    """Flat playlist listing (ids and titles only — no per-video requests),
    through the same cookie chain as downloads."""
    from ..ingest.ingest import (_BotWall, _auth_strategies, _classify, _clean_err,
                                 _extractor_args, _require_ytdlp)
    yt_dlp = _require_ytdlp()
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "playlistend": int(limit),
            "socket_timeout": int(cfg.get("ingest.socket_timeout", 120) or 120),
            "extractor_args": _extractor_args(cfg)}
    last: Exception | None = None
    saw_bot = False
    for label, overlay in _auth_strategies(cfg):
        try:
            with yt_dlp.YoutubeDL({**opts, **overlay}) as ydl:
                info = ydl.extract_info(url, download=False)
            return info or {}
        except Exception as e:  # noqa: BLE001 - classified below
            c = _classify(e)
            last = e
            if isinstance(c, _BotWall):
                saw_bot = True
                log.warning("shorts: listing via '%s' hit the bot wall; trying next", label)
                continue
            msg = _clean_err(e)
            if "does not have a shorts tab" in msg.lower():
                raise ShortForgeError("this channel has no Shorts") from e
            log.warning("shorts: listing via '%s' failed (%s); trying next", label, msg[:160])
    if saw_bot:
        from ..ingest.ingest import BOT_AUTH_MESSAGE
        raise ShortForgeError(BOT_AUTH_MESSAGE) from last
    raise ShortForgeError(f"could not list this channel: "
                          f"{(str(last) if last else 'unknown error')[:200]}") from last


def channel_name_from(info: dict) -> str:
    name = info.get("channel") or info.get("uploader") or ""
    if not name:
        name = re.sub(r"\s*-\s*Shorts\s*$", "", info.get("title") or "").strip()
    return name


def list_new_shorts(channel_url: str, cfg: Config, want: int, skip: set[str], *,
                    max_scan: int = 3000) -> tuple[dict, list[dict], bool]:
    """The ``want`` newest Shorts of a channel that are not in ``skip``.

    Returns ``(channel, entries, exhausted)``; ``exhausted`` means the channel
    ran out before ``want`` new ones were found. The listing window grows only
    as far as needed: a channel whose newest 40 are already downloaded costs one
    request for 40+N entries, not a scan of its whole back catalogue.
    """
    want = max(1, int(want))
    window = min(max_scan, want + 30)
    while True:
        info = _flat_list(store.shorts_tab_url(channel_url), cfg, window)
        entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
        new = [e for e in entries if e["id"] not in skip][:want]
        more_exist = len(entries) >= window
        if len(new) >= want or not more_exist or window >= max_scan:
            channel = {"name": channel_name_from(info),
                       "id": info.get("channel_id") or info.get("id") or ""}
            return channel, new, len(new) < want
        window = min(max_scan, window * 3)


# --- metadata: every Short gets a title, a description and hashtags ---------- #

def _norm_tag(raw: str) -> str:
    t = re.sub(r"[^\w]", "", raw or "", flags=re.UNICODE)
    return t if 2 <= len(t) <= 40 else ""


def hashtags_for(info: dict, channel_name: str = "", limit: int = 15) -> tuple[list[str], str]:
    """``(hashtags, source)`` — the uploader's own where they wrote any.

    Order: hashtags in the title/description (what the creator chose to show),
    then the video's keyword tags. Only if there are none at all are they made
    from the title, and ``source`` says so — the sidecar must never pass off
    invented hashtags as the uploader's.
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(tag: str):
        t = _norm_tag(tag)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append("#" + t)

    for text in (info.get("title") or "", info.get("description") or ""):
        for m in _HASHTAG.finditer(text):
            add(m.group(1))
    source = "uploader" if out else ""
    for tag in info.get("tags") or []:
        add(str(tag))
    if out:
        return out[:limit], source or "tags"

    words = [w for w in re.findall(r"[^\W\d_]{4,}", info.get("title") or "", re.UNICODE)
             if w.lower() not in _STOPWORDS]
    for w in words[:5]:
        add(w)
    if channel_name:
        add(channel_name)
    add("shorts")
    return out[:limit], "generated"


def description_for(info: dict) -> tuple[str, str]:
    desc = (info.get("description") or "").strip()
    if desc:
        return desc, "uploader"
    return (info.get("title") or "").strip(), "generated"


def _ai_fill(title: str, channel: str) -> dict | None:
    """Optional: ask the configured LLM for a description + hashtags."""
    try:
        from ..providers import call_task_chat
        from ..providers.store import load_store
        prompt = ("Write metadata for a YouTube Short. Reply with JSON only: "
                  '{"description": "<1-2 sentences>", "hashtags": ["#a", "#b", ...]} '
                  "with 5-10 relevant hashtags.\n"
                  f"Title: {title[:200]}\nChannel: {channel[:80]}")
        content, _m, _f = call_task_chat(load_store(), "metadata",
                                         [{"role": "user", "content": prompt}],
                                         max_tokens=300, timeout=60, retries=0)
        m = re.search(r"\{.*\}", content or "", re.S)
        data = json.loads(m.group(0)) if m else None
        if isinstance(data, dict) and data.get("description"):
            tags = [("#" + _norm_tag(str(t).lstrip("#"))) for t in data.get("hashtags") or []]
            return {"description": str(data["description"]).strip(),
                    "hashtags": [t for t in tags if len(t) > 1][:15]}
    except Exception as e:  # noqa: BLE001 - an optional nicety, never a blocker
        log.info("shorts: AI metadata unavailable (%s) — keeping the built-in text", e)
    return None


def write_sidecars(video_path: str, rec: dict) -> tuple[str, str]:
    """``<name>.txt`` (ready to paste: title / description / hashtags) and
    ``<name>.json`` (everything known about the Short)."""
    base = os.path.splitext(video_path)[0]
    txt = base + ".txt"
    lines = [
        "TITLE", rec["title"], "",
        "DESCRIPTION", rec["description"], "",
        "HASHTAGS", " ".join(rec["hashtags"]), "",
        "SOURCE", rec["url"],
        f"Channel: {rec.get('channel_name') or '-'}   "
        f"Uploaded: {rec.get('upload_date') or '-'}   "
        f"Duration: {rec.get('duration') or '-'}s   "
        f"Resolution: {rec.get('width') or '?'}x{rec.get('height') or '?'}",
    ]
    if rec.get("description_source") != "uploader" or rec.get("hashtags_source") == "generated":
        lines += ["", "NOTE: " + "; ".join(filter(None, [
            ("description written by ShortForge — the uploader left it empty"
             if rec.get("description_source") != "uploader" else ""),
            ("hashtags written by ShortForge — the uploader used none"
             if rec.get("hashtags_source") in ("generated", "ai") else ""),
        ]))]
    with open(txt, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    js = base + ".json"
    with open(js, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    return txt, js


# --- downloading one Short --------------------------------------------------- #

def format_chain(st: dict) -> list[str]:
    cap = str(st.get("max_height") or "best").lower()
    if cap.isdigit():
        h = int(cap)
        return [f"bv*[height<={h}]+ba/b[height<={h}]", "bv*+ba/b", "b"]
    return ["bv*+ba/b", "b"]


def _container(st: dict) -> str:
    return "mkv" if str(st.get("container")).lower() == "mkv" else "mp4"


def base_opts(cfg: Config, st: dict, staging: str, progress_hook=None) -> dict:
    """yt-dlp options. Everything is written into ``staging`` — a private folder
    per Short inside shorts_data — never straight into the output folder.

    Writing into the output folder is what left it full of debris: an attempt
    YouTube cut off halfway leaves an 80-90 MB ``.part``, a failed merge leaves
    a video-only ``.f137.mp4``, and the fallback that finally works saves under
    a different name, so the leftovers were never cleaned up. Now only the
    finished video is ever moved out.
    """
    from ..ingest.ingest import _base_opts
    opts = _base_opts(cfg, staging)
    opts.update({
        "outtmpl": os.path.join(staging, "%(id)s.%(ext)s"),
        "merge_output_format": _container(st),
        "overwrites": False,
    })
    if str(st.get("codec")).lower() == "compatible":
        # Resolution and frame rate FIRST, so preferring H.264 can never cost a
        # single pixel — it only breaks ties between same-size formats.
        opts["format_sort"] = ["res", "fps", "vcodec:h264", "acodec:aac"]
    if st.get("save_thumbnail"):
        opts["writethumbnail"] = True
        opts["postprocessors"] = [{"key": "FFmpegThumbnailsConvertor", "format": "jpg",
                                   "when": "before_dl"}]
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts


def _staged_video(info: dict, staging: str, container: str) -> str | None:
    for d in info.get("requested_downloads") or []:
        p = d.get("filepath")
        if p and os.path.isfile(p):
            return p
    vid = info.get("id") or ""
    hits = [p for p in glob.glob(os.path.join(glob.escape(staging), f"{glob.escape(vid)}.*"))
            if p.lower().endswith((".mp4", ".mkv", ".webm", ".m4v", ".mov"))]
    hits.sort(key=lambda p: (not p.lower().endswith("." + container), p))
    return hits[0] if hits else None


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_title(title: str, limit: int = 90) -> str:
    """A title usable as a Windows file name: reserved characters out, no
    trailing dot or space (Explorer can't open those), and not too long."""
    t = _BAD_CHARS.sub(" ", title or "")
    t = " ".join(t.split())[:limit].rstrip(". ")
    return t


def numbered_name(number: int, title: str, st: dict) -> str:
    """``7 - Title`` (or just ``7``): the number is the download order in that
    folder, so the files sort in the order they arrived — which matters when a
    creator gives every Short the same title."""
    if str(st.get("name_style") or "number_title") == "number":
        return str(number)
    t = safe_title(title)
    return f"{number} - {t}" if t else str(number)


def embed_metadata(src: str, dst: str, rec: dict) -> bool:
    """Copy ``src`` to ``dst`` with title, description and hashtags written INTO
    the file. Stream copy only — not a single frame is re-encoded.

    This is what lets the output folder hold nothing but videos while every
    Short still carries its title, description and hashtags (Windows shows them
    under Properties → Details).
    """
    from ..utils import require_binary, run
    tags = " ".join(rec.get("hashtags") or [])
    desc = rec.get("description") or ""
    if tags and tags not in desc:
        desc = f"{desc}\n\n{tags}".strip()
    cmd = [require_binary("ffmpeg"), "-v", "error", "-y", "-i", src,
           "-map", "0", "-c", "copy", "-map_metadata", "0",
           "-metadata", f"title={rec.get('title') or ''}",
           "-metadata", f"description={desc}",
           "-metadata", f"synopsis={desc}",
           "-metadata", f"comment={tags}",
           "-metadata", f"artist={rec.get('channel_name') or ''}"]
    if rec.get("upload_date"):
        cmd += ["-metadata", f"date={rec['upload_date']}"]
    if dst.lower().endswith(".mp4"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(dst)
    try:
        run(cmd)
        return os.path.isfile(dst) and os.path.getsize(dst) > 0
    except Exception as e:  # noqa: BLE001 - the video itself is fine without tags
        log.warning("shorts: could not write the title/description into the file "
                    "(%s) — keeping the video as downloaded", str(e)[:200])
        try:
            os.remove(dst)
        except OSError:
            pass
        return False


def _quality_hook(seen: list, outer=None):
    """Remember the tallest stream yt-dlp STARTED downloading. If the file that
    finally lands is shorter, YouTube cut the better stream off and a fallback
    route delivered less — which must be said, not hidden."""
    def hook(d: dict) -> None:
        h = (d.get("info_dict") or {}).get("height")
        if h:
            seen.append(int(h))
        if outer:
            outer(d)
    return hook


def download_short(item: dict, cfg: Config, st: dict, progress_hook=None) -> dict:
    """Download one Short; return its history record. Raises ShortForgeError.

    Only the finished video reaches the output folder, named in download order
    (``N - Title.mp4``). Sidecar ``.txt``/``.json``/``.jpg`` files are written
    only if switched on in Settings — the default output is videos alone.
    """
    import shutil
    from ..ingest.ingest import download_resilient

    dd = store.data_dir(cfg)
    staging = os.path.join(dd, "incoming", item["id"])
    os.makedirs(staging, exist_ok=True)
    attempted: list[int] = []
    opts = base_opts(cfg, st, staging, _quality_hook(attempted, progress_hook))
    container = _container(st)
    t0 = time.time()
    try:
        info, _prepared = download_resilient(item["url"], cfg, opts, formats=format_chain(st))
    except BaseException:
        # A download that FAILED leaves nothing behind. (A power cut never gets
        # here, so its partial file stays in staging and is resumed next time.)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    staged = _staged_video(info, staging, container)
    if not staged:
        shutil.rmtree(staging, ignore_errors=True)
        raise ShortForgeError(f"downloaded {item['id']} but the video file is missing")

    channel_name = item.get("channel_name") or info.get("channel") or info.get("uploader") or ""
    title = (info.get("title") or item.get("title") or item["id"]).strip()
    description, d_src = description_for(info)
    hashtags, h_src = hashtags_for(info, channel_name)
    if st.get("ai_fill_missing") and (d_src != "uploader" or h_src == "generated"):
        ai = _ai_fill(title, channel_name)
        if ai:
            if d_src != "uploader":
                description, d_src = ai["description"], "ai"
            if h_src == "generated" and ai["hashtags"]:
                hashtags, h_src = ai["hashtags"], "ai"

    # Resolution/duration straight from the file when YouTube's metadata lacks
    # them — the history is where the operator checks "did I get full quality?".
    w, h, dur = info.get("width"), info.get("height"), info.get("duration")
    if not (w and h and dur):
        try:
            from ..utils import ffprobe_info
            p = ffprobe_info(staged)
            w, h, dur = w or p.width, h or p.height, dur or round(p.duration, 1)
        except Exception:  # noqa: BLE001 - metadata only; the file is fine
            pass

    rec = {
        "id": info.get("id") or item["id"],
        "url": info.get("webpage_url") or item["url"],
        "title": title,
        "description": description, "description_source": d_src,
        "hashtags": hashtags, "hashtags_source": h_src,
        "tags": list(info.get("tags") or [])[:40],
        "channel_key": item.get("channel_key") or "",
        "channel_name": channel_name,
        "channel_url": info.get("channel_url") or "",
        "uploader": info.get("uploader") or "",
        "upload_date": _date(info.get("upload_date")),
        "duration": dur,
        "width": w, "height": h, "fps": info.get("fps"),
        "vcodec": info.get("vcodec"), "acodec": info.get("acodec"),
        "format": info.get("format_id") or info.get("format"),
        "view_count": info.get("view_count"), "like_count": info.get("like_count"),
        "downloaded_at": time.time(),
        "seconds": round(time.time() - t0, 1),
        "source": item.get("source") or "channel",
        "quality_note": "",
    }
    tallest = max(attempted, default=0)
    if h and tallest > int(h) + 8:
        rec["quality_note"] = (f"YouTube cut off the {tallest}p stream partway; "
                               f"this copy is {h}p from a fallback route")
        log.warning("shorts: %s — %s", rec["id"], rec["quality_note"])

    # Name it by download order in its folder, then move ONLY the video out.
    dest = destination(cfg, st, item)
    os.makedirs(dest, exist_ok=True)
    number = store.next_number(dd, dest)
    base = numbered_name(number, title, st)
    ext = os.path.splitext(staged)[1].lower() or f".{container}"
    final = os.path.join(dest, base + ext)
    if st.get("embed_metadata", True):
        tagged = os.path.join(staging, "tagged" + ext)
        if embed_metadata(staged, tagged, rec):
            staged = tagged
    shutil.move(staged, final)
    store.commit_number(dd, dest, number)

    rec.update({"number": number, "file": os.path.abspath(final),
                "filesize": os.path.getsize(final), "thumbnail": "", "sidecar": "",
                "info_json": ""})
    thumb = os.path.join(staging, f"{item['id']}.jpg")
    if st.get("save_thumbnail") and os.path.isfile(thumb):
        rec["thumbnail"] = os.path.abspath(os.path.join(dest, base + ".jpg"))
        shutil.move(thumb, rec["thumbnail"])
    if st.get("sidecar_txt") or st.get("sidecar_json"):
        txt, js = write_sidecars(final, rec)
        rec["sidecar"] = txt if st.get("sidecar_txt") else ""
        rec["info_json"] = js if st.get("sidecar_json") else ""
        for path, keep in ((txt, st.get("sidecar_txt")), (js, st.get("sidecar_json"))):
            if not keep:
                try:
                    os.remove(path)
                except OSError:
                    pass
    shutil.rmtree(staging, ignore_errors=True)

    log.info("shorts: %s — #%d %sx%s %s, %.1f MB in %.0fs → %s", rec["id"], number,
             rec["width"], rec["height"], rec["vcodec"], rec["filesize"] / 1e6,
             rec["seconds"], os.path.basename(final))
    return rec


def _date(raw) -> str:
    s = str(raw or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else ""
