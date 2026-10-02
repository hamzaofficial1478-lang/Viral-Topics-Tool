"""Find Shorts channels — from a description, or "more channels like this one".

How a search runs (each step is what makes the next one trustworthy):

1. **Work out what to look for.** From a description: its key words. From a
   reference channel: its own Short titles, hashtags and About text, plus its
   language and country (so "same as the reference" is possible). When an AI
   model is set up it also writes search phrases — in the target language,
   which matters: YouTube's channel search barely changes with region (tested:
   PK, SA and BR returned the same channels as US), so reaching channels from
   another country means searching in its language and naming it.
2. **Collect candidates** from YouTube's channel search and from ordinary video
   search (channels whose videos match), plus — for a reference — the channels
   it features. Already-saved, wishlisted and dismissed channels are skipped.
3. **Check every candidate** before showing it: it must actually post Shorts
   (its Shorts tab is listed), its language is detected from those titles, and
   its country is read from its About page — the channel's own setting, not a
   guess. With AI on, a final pass judges whether each one really fits.
4. **Report honestly.** Results stream in as they are confirmed. If fewer than
   asked for are found, the summary says so and why the rest were turned down.

A search is a background job (``cli.py shorts discover-run``), written to
``shorts_data/discover/<id>.json`` after every step, so closing the browser tab
doesn't stop it and the dashboard can show it live.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from ..config import Config
from ..utils import ShortForgeError, log, write_json_atomic
from . import countries as CO
from . import langid as LG
from . import store

DISCOVER_DIR = "discover"
DISMISSED_FILE = "dismissed.json"
_CHANNEL_FILTER = "EgIQAg%253D%253D"         # YouTube search: channels only
KEEP_JOBS = 15

# Hashtags that say nothing about a channel's topic.
_GENERIC_TAGS = {
    "shorts", "short", "ytshorts", "youtubeshorts", "shortsvideo", "shortvideo", "viral",
    "viralshorts", "viralvideo", "trending", "trend", "fyp", "foryou", "foryoupage", "explore",
    "explorepage", "reels", "reel", "subscribe", "youtube", "tiktok", "new", "video", "funny",
    "comedy", "love", "instagram", "like", "follow", "shortsfeed", "feed", "tranding",
}
_EXTRA_STOP = set("""
video videos short shorts channel part new best top watch subscribe like follow full
episode official real latest today day time way make made making get got one two three
this that with from your what when how why who which will just also into over more
على التي الذي هذا هذه هذي كيف اللي إلى الى عند كان كل مع بعد قبل عشان والله فيديو
اند ولا اكسبلور ترند لايك متابعة شورت شورتس explore
کیسے ہے ہیں کے کی کا اور میں سے کو یہ وہ بھی نہیں آپ ایک کریں ویڈیو
है हैं के की का और में से को यह वह भी नहीं एक वीडियो
""".split())

REASONS = {
    "no_shorts": "posts no Shorts",
    "too_few_shorts": "fewer Shorts than your minimum",
    "too_small": "below your minimum subscribers",
    "wrong_language": "Shorts are in a different language",
    "unknown_language": "language couldn't be told from its titles",
    "wrong_country": "based in a different country",
    "no_country": "doesn't list its country",
    "not_a_match": "AI judged it off-topic",
    "unreachable": "couldn't be read from YouTube",
}


# --- small helpers -------------------------------------------------------------- #

def _dir(dd: str) -> str:
    d = os.path.join(dd, DISCOVER_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _stops() -> set[str]:
    s = set(_EXTRA_STOP)
    for words in LG._STOP_SETS.values():
        s |= words
    return s


_STOPWORDS = _stops()
_TOKEN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def tokens(text: str) -> list[str]:
    """Meaningful words, lower-cased, in order, no repeats."""
    seen, out = set(), []
    for w in _TOKEN.findall(re.sub(r"https?://\S+", " ", text or "")):
        w = w.lower()
        if w in _STOPWORDS or w in _GENERIC_TAGS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def hashtags(texts: list[str]) -> Counter:
    c: Counter = Counter()
    for t in texts:
        for tag in re.findall(r"#([^\s#.,!?;:()\[\]{}\"'<>|/\\]{2,40})", t or ""):
            tag = tag.lower()
            if tag not in _GENERIC_TAGS and not tag.isdigit():
                c[tag] += 1
    return c


def _json_block(text: str):
    """First JSON object/array in an AI reply (models wrap it in prose/fences)."""
    if not text:
        return None
    for opener, closer in (("{", "}"), ("[", "]")):
        i = text.find(opener)
        j = text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except ValueError:
                continue
    return None


# --- talking to YouTube ----------------------------------------------------------- #

def _listing(url: str, cfg: Config, limit: int, lang: str = "") -> dict:
    from .youtube import _flat_list
    return _flat_list(url, cfg, limit, quiet_errors=True,
                      youtube_args={"lang": [lang]} if lang else None)


# Our language codes → YouTube's interface-language codes. Only used on SEARCH
# requests: it nudges the ranking toward that language (measured: 1 Arabic
# channel in 12 vs 0). Never used when reading a channel's own Shorts — YouTube
# then shows creator-supplied TRANSLATED titles, which would fool the language
# check into seeing Arabic on an English channel.
_HL = {"he": "iw", "zh": "zh-CN", "tl": "fil", "hi-Latn": ""}


def _hl_for(language: str) -> str:
    if not language:
        return ""
    code = _HL.get(language, language)
    try:
        from yt_dlp.extractor.youtube._base import YoutubeBaseInfoExtractor as _B
        ok = set(_B._SUPPORTED_LANG_CODES)
    except Exception:  # noqa: BLE001 - moved in some yt-dlp version: skip the hint
        return ""
    return code if code in ok else ""


SAMPLE = 15        # Shorts read per channel for its titles, language and views


def count_shorts(channel_url: str, cfg: Config, at_least: int) -> int:
    """How many Shorts the channel has, counting no further than ``at_least``."""
    info = _listing(store.shorts_tab_url(channel_url), cfg, int(at_least))
    return len([e for e in info.get("entries") or [] if e])


def search_channels(query: str, cfg: Config, limit: int = 30, lang: str = "") -> list[dict]:
    """YouTube channel search → [{id, url, name, handle, subscribers, description}]."""
    url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
           + "&sp=" + _CHANNEL_FILTER)
    out = []
    for e in _listing(url, cfg, limit, lang).get("entries") or []:
        if not e or e.get("ie_key") != "YoutubeTab" or not str(e.get("id", "")).startswith("UC"):
            continue
        out.append({"id": e["id"], "url": e.get("url") or f"https://www.youtube.com/channel/{e['id']}",
                    "name": e.get("title") or e.get("channel") or "",
                    "handle": e.get("uploader_id") or "",
                    "subscribers": e.get("channel_follower_count"),
                    "description": e.get("description") or "",
                    "avatar": _thumb(e)})
    return out


def search_video_channels(query: str, cfg: Config, limit: int = 30,
                          lang: str = "") -> list[dict]:
    """Channels behind the videos a search finds — catches creators whose
    channel NAME doesn't contain the topic words."""
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query + " #shorts")
    out, seen = [], set()
    for e in _listing(url, cfg, limit, lang).get("entries") or []:
        cid = (e or {}).get("channel_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "url": e.get("channel_url") or f"https://www.youtube.com/channel/{cid}",
                    "name": e.get("channel") or "", "handle": e.get("uploader_id") or "",
                    "subscribers": None, "description": "", "avatar": ""})
    return out


def featured_channels(channel_url: str, cfg: Config) -> list[dict]:
    """Channels the reference channel itself recommends (not every channel has any)."""
    try:
        info = _listing(channel_url.rstrip("/") + "/channels", cfg, 30)
    except ShortForgeError:
        return []
    out = []
    for e in info.get("entries") or []:
        if e and str(e.get("id", "")).startswith("UC"):
            out.append({"id": e["id"], "url": e.get("url") or f"https://www.youtube.com/channel/{e['id']}",
                        "name": e.get("title") or "", "handle": e.get("uploader_id") or "",
                        "subscribers": e.get("channel_follower_count"), "description": "",
                        "avatar": _thumb(e)})
    return out


def _thumb(e: dict) -> str:
    th = [t for t in (e.get("thumbnails") or []) if t.get("url")]
    if not th:
        return ""
    u = th[-1]["url"]
    return ("https:" + u) if u.startswith("//") else u


def fetch_about(channel_url: str, cfg: Config) -> dict:
    """The channel's own About data: country, description, subscriber and video
    counts. Read from the About page in English, so the country is a name that
    `countries.code_for` understands."""
    from ..ingest.ingest import _auth_strategies, _require_ytdlp
    yt_dlp = _require_ytdlp()
    url = channel_url.rstrip("/") + "/about?hl=en"
    html = ""
    last = None
    for _label, overlay in _auth_strategies(cfg):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                   "socket_timeout": 30, **overlay}) as y:
                html = y.urlopen(url).read().decode("utf-8", "replace")
            break
        except Exception as e:  # noqa: BLE001 - next cookie strategy
            last = e
    if not html:
        raise ShortForgeError(f"couldn't open the About page ({str(last)[:120]})")
    about = _about_from_html(html)
    return about


def _about_from_html(html: str) -> dict:
    out = {"country": "", "description": "", "subscribers_text": "", "videos_text": "",
           "joined": "", "name": "", "handle": ""}
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    model = None
    if m:
        try:
            data = json.loads(m.group(1))
            stack = [data]
            while stack and model is None:
                o = stack.pop()
                if isinstance(o, dict):
                    if "aboutChannelViewModel" in o:
                        model = o["aboutChannelViewModel"]
                        break
                    stack.extend(o.values())
                elif isinstance(o, list):
                    stack.extend(o)
        except ValueError:
            model = None
    if model:
        def txt(v):
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                return v.get("content") or v.get("simpleText") or ""
            return ""
        out["country"] = txt(model.get("country"))
        out["description"] = txt(model.get("description"))
        out["subscribers_text"] = txt(model.get("subscriberCountText"))
        out["videos_text"] = txt(model.get("videoCountText"))
        out["joined"] = txt(model.get("joinedDateText"))
        out["handle"] = txt(model.get("canonicalChannelUrl")).rsplit("/", 1)[-1]
    elif "aboutChannelViewModel" in html:          # JSON moved; the field didn't
        c = re.search(r'"aboutChannelViewModel":\{.*?"country":"([^"]{2,60})"', html, re.S)
        out["country"] = c.group(1) if c else ""
    return out


# --- AI (optional) ---------------------------------------------------------------- #

def _ai(messages: list, max_tokens: int = 900) -> str:
    from ..providers import call_task_chat
    from ..providers.store import load_store
    content, _m, _f = call_task_chat(load_store(), "channel_discovery", messages,
                                     max_tokens=max_tokens, timeout=180, retries=1)
    return content or ""


def ai_plan(want: str, country: str, language: str) -> dict:
    """Search phrases (in the target language where natural) + topic keywords."""
    lang = LG._LABEL.get(language, "") if language else "any language"
    ctry = CO.name(country) if country else "any country"
    prompt = (
        "You help find YouTube Shorts channels. The user wants channels like this:\n"
        f"{want[:1500]}\n\nTarget language: {lang}. Target country: {ctry}.\n"
        "Write 6 short YouTube search phrases (2-5 words each) that would find such "
        "channels. Write most of them in the target language (in its usual script, plus "
        "one in Roman script for Urdu/Hindi), and include the country or a local word "
        "where it helps. Also give 8 topic keywords in English.\n"
        'Reply with JSON only: {"queries": ["..."], "keywords": ["..."], '
        '"summary": "one sentence describing the wanted channels"}')
    data = _json_block(_ai([{"role": "user", "content": prompt}], 600))
    if not isinstance(data, dict) or not data.get("queries"):
        raise ShortForgeError("the AI reply had no search phrases")
    return {"queries": [str(q).strip()[:80] for q in data["queries"] if str(q).strip()][:8],
            "keywords": [str(k).strip().lower() for k in data.get("keywords") or []][:12],
            "summary": str(data.get("summary") or "")[:300]}


def ai_judge(want: str, language: str, cands: list[dict]) -> dict[str, dict]:
    """{channel_id: {"fit": 0-10, "language": code, "why": str}} for a batch."""
    lang = LG._LABEL.get(language, "") if language else "any"
    rows = []
    for i, c in enumerate(cands):
        rows.append(f'{i}. "{c["name"][:60]}" — about: {(c.get("description") or "")[:160]!r} — '
                    f'Shorts: ' + " | ".join(t[:70] for t in c.get("titles", [])[:6]))
    prompt = (
        "The user is looking for YouTube Shorts channels like this:\n"
        f"{want[:1200]}\nWanted language: {lang}.\n\nCandidates:\n" + "\n".join(rows) +
        "\n\nFor each candidate, rate how well it fits what the user wants, 0-10 "
        "(same topic and style = high; merely related = mid; different topic = low), "
        "say which language its Shorts are in (ISO code, or 'hi-Latn' for Hindi/Urdu "
        "written in Latin letters), and give a reason of at most 10 words.\n"
        'Reply with JSON only: [{"i": 0, "fit": 7, "language": "en", "why": "..."}]')
    data = _json_block(_ai([{"role": "user", "content": prompt}], 1200))
    out = {}
    if isinstance(data, dict):
        data = data.get("results") or data.get("candidates") or []
    for row in data or []:
        try:
            i = int(row.get("i"))
            c = cands[i]
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        try:
            fit = max(0, min(10, int(round(float(row.get("fit"))))))
        except (TypeError, ValueError):
            continue
        out[c["id"]] = {"fit": fit, "language": str(row.get("language") or "")[:8],
                        "why": str(row.get("why") or "")[:120]}
    if not out:
        raise ShortForgeError("the AI reply couldn't be read")
    return out


def ai_available() -> bool:
    try:
        from ..providers.store import load_store, resolve_task
        return bool(resolve_task(load_store(), "channel_discovery"))
    except Exception:  # noqa: BLE001
        return False


# --- planning without AI ------------------------------------------------------- #

def plan_queries(want_text: str, keywords: list[str], tags: list[str], country: str,
                 language: str, *, from_reference: bool) -> list[str]:
    """Search phrases when no AI is available (or as extras when it is)."""
    qs: list[str] = []
    ctry = CO.name(country) if country else ""
    lang = LG._LABEL.get(language, "") if language else ""
    lang = lang.split(" ")[0] if lang else ""
    if not from_reference and want_text.strip():
        qs.append(" ".join(want_text.split())[:80])
    core = " ".join(keywords[:3])
    for t in tags[:3]:
        qs.append(t)
    if core:
        qs.append(core)
        if ctry:
            qs.append(f"{core} {ctry}")
        if lang and language not in ("en", ""):
            qs.append(f"{core} {lang}")
    if len(keywords) > 3:
        qs.append(" ".join(keywords[3:6]))
    out = []
    for q in qs:
        q = q.strip()
        if q and q.lower() not in {x.lower() for x in out}:
            out.append(q)
    return out[:8]


_NON_LATIN = {"ar", "ur", "fa", "hi", "bn", "pa", "ta", "te", "ml", "kn", "gu", "mr", "ja",
              "ko", "zh", "th", "ru", "uk", "el", "he"}


def learned_queries(titles_by_channel: dict[str, list[str]], language: str,
                    used: list[str], limit: int = 4,
                    detected: dict[str, str] | None = None) -> list[str]:
    """Search phrases in the TARGET language, learned from channels already
    found in it — no translation service, no AI.

    Measured: English words find English channels whatever language is asked
    for ("cooking recipes" + Arabic → 0 of 48 checked were Arabic), while the
    same topic in Arabic words ("وصفات طبخ") returns mostly Arabic channels. So
    once a search turns up even one channel in the wanted language, the words
    its Shorts titles share become the next searches. A word counts once per
    channel, so one prolific channel can't dominate.

    A channel written in its own script (Arabic, Devanagari …) contributes
    only words in that script — its Latin words are the English mixed in. A
    channel written in Latin letters contributes those: Urdu or Hindi typed in
    Roman script ("biryani kaise banaye") is still the wanted language.
    ``detected`` maps channel → the language detected for it; a channel with
    no entry is taken to be written like ``language``.
    """
    seen_q = " ".join(used).lower()
    channels_using: Counter = Counter()      # how many channels use the word
    uses: Counter = Counter()                # how often, overall
    for key, titles in titles_by_channel.items():
        own_script = (detected or {}).get(key) or language
        words: list[str] = []
        for t in titles:
            # tokens() de-duplicates per call, so this counts per TITLE
            words.extend(w for w in tokens(t)
                         if not (own_script in _NON_LATIN and w.isascii()))
        uses.update(words)
        channels_using.update(set(words))
    # Words several channels share rank first; a word only one channel uses can
    # still be learned (two channels often share no word at all), ranked by use.
    ranked = []
    for w in sorted(uses, key=lambda w: (channels_using[w], uses[w]), reverse=True):
        if w in seen_q or uses[w] < 2:
            continue
        ranked.append(w)
        if len(ranked) >= 12:
            break
    pairs = [(0, 1), (2, 3), (0, 2), (1, 3), (4, 5), (0, 3)]
    out = []
    for a, b in pairs:
        if b < len(ranked):
            q = f"{ranked[a]} {ranked[b]}"
            if q not in out:
                out.append(q)
        if len(out) >= limit:
            break
    if not out and ranked:
        out = ranked[:limit]
    return out


def keyword_score(cand: dict, keywords: list[str]) -> tuple[int, list[str]]:
    hay = " ".join([cand.get("name", ""), cand.get("description", "")]
                   + list(cand.get("titles", []))).lower()
    hit = [k for k in keywords if k and k in hay]
    return len(hit), hit


# --- jobs ---------------------------------------------------------------------- #

def job_path(dd: str, job_id: str) -> str:
    return os.path.join(_dir(dd), f"{job_id}.json")


def load_job(dd: str, job_id: str) -> dict | None:
    try:
        with open(job_path(dd, job_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_job(dd: str, job: dict) -> None:
    job["updated"] = time.time()
    write_json_atomic(job_path(dd, job["id"]), job)


_FINAL = ("done", "failed", "cancelled")


def list_jobs(dd: str) -> list[dict]:
    out = []
    for name in os.listdir(_dir(dd)):
        if name.endswith(".json") and name != DISMISSED_FILE:
            j = load_job(dd, name[:-5])
            if j and j.get("id"):
                out.append(j)
    out.sort(key=lambda j: j.get("created") or 0, reverse=True)
    return out


def new_job(dd: str, params: dict) -> dict:
    p = {
        "mode": "like" if params.get("reference") and params.get("mode") != "describe" else "describe",
        "prompt": str(params.get("prompt") or "").strip()[:2000],
        "reference": str(params.get("reference") or "").strip(),
        "count": max(1, min(int(params.get("count") or 10), 50)),
        "country": params.get("country") or "",           # "", a code, or "same"
        "language": params.get("language") or "",         # "", a code, or "same"
        "min_subscribers": max(0, int(params.get("min_subscribers") or 0)),
        "min_shorts": max(1, int(params.get("min_shorts") or 3)),
        "use_ai": bool(params.get("use_ai", True)),
        "allow_unlisted_country": bool(params.get("allow_unlisted_country", True)),
        "exclude_ids": list(params.get("exclude_ids") or [])[:500],
    }
    if p["mode"] == "describe" and not p["prompt"]:
        raise ShortForgeError("describe the channels you want, or give a reference channel")
    if p["mode"] == "like":
        store.parse_channel(p["reference"])                # raises ValueError with a reason
    job = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4],
           "created": time.time(), "status": "queued", "stage": "waiting to start",
           "params": p, "progress": {}, "results": [], "rejected": {}, "notes": [],
           "queries": [], "pid": None}
    save_job(dd, job)
    _prune(dd)
    return job


def _prune(dd: str) -> None:
    for j in list_jobs(dd)[KEEP_JOBS:]:
        if j.get("status") not in ("running", "queued"):
            try:
                os.remove(job_path(dd, j["id"]))
            except OSError:
                pass


def cancel(dd: str, job_id: str) -> None:
    open(os.path.join(_dir(dd), f"{job_id}.cancel"), "w").close()


def _cancelled(dd: str, job_id: str) -> bool:
    return os.path.exists(os.path.join(_dir(dd), f"{job_id}.cancel"))


def is_alive(job: dict) -> bool:
    """A running job whose process still exists and has written recently."""
    from ..utils import pid_alive
    if job.get("status") not in ("running", "queued"):
        return False
    if job.get("status") == "queued":
        return time.time() - (job.get("created") or 0) < 60
    return bool(job.get("pid")) and pid_alive(int(job["pid"])) and \
        time.time() - (job.get("updated") or 0) < 300


def spawn(dd: str, job_id: str) -> None:
    """Run the job in its own process, detached from the dashboard, so closing
    the tab — or the whole browser — doesn't stop the search."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    kwargs: dict = {"cwd": here, "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.join(here, "cli.py"), "shorts", "discover-run",
                      "--job", job_id], **kwargs)


# --- dismissed (never suggest again) ------------------------------------------- #

def dismissed(dd: str) -> set[str]:
    try:
        with open(os.path.join(_dir(dd), DISMISSED_FILE), "r", encoding="utf-8") as f:
            return set(json.load(f).get("ids") or [])
    except (OSError, ValueError):
        return set()


def dismiss(dd: str, channel_ids: list[str]) -> None:
    ids = dismissed(dd) | set(channel_ids)
    write_json_atomic(os.path.join(_dir(dd), DISMISSED_FILE), {"ids": sorted(ids)})


# --- the search itself ----------------------------------------------------------- #

class _Job:
    """Owns the job dict and writes it to disk; thread-safe."""

    def __init__(self, dd: str, job: dict):
        self.dd, self.job = dd, job
        self.lock = threading.Lock()

    def update(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                if k == "progress":
                    self.job.setdefault("progress", {}).update(v)
                else:
                    self.job[k] = v
            self._save(final=self.job.get("status") in _FINAL)

    def reject(self, reason: str) -> None:
        with self.lock:
            r = self.job.setdefault("rejected", {})
            r[reason] = r.get(reason, 0) + 1

    def note(self, text: str) -> None:
        log.info("discover: %s", text)
        with self.lock:
            self.job.setdefault("notes", []).append(text)
            self._save()

    def _save(self, final: bool = False) -> None:
        """A progress save that can't get at the file (Windows: the dashboard
        or OneDrive has it open for longer than the retry waits) is skipped —
        the next save writes everything anyway. It used to end the whole
        search with "Access is denied". The FINAL save must land, so it keeps
        trying for longer and only then gives up."""
        try:
            save_job(self.dd, self.job)
        except PermissionError as e:
            if not final:
                log.warning("discover: couldn't save progress just now (%s) — the next "
                            "update will", e)
                return
            time.sleep(2)
            save_job(self.dd, self.job)


def _known(dd: str) -> tuple[set[str], set[str]]:
    keys, ids = set(), set()
    for c in store.load_channels(dd)["channels"]:
        keys.add(c["key"])
        if c.get("channel_id"):
            ids.add(c["channel_id"])
    return keys, ids


def _key_for(cand: dict) -> tuple[str, str]:
    h = (cand.get("handle") or "").strip()
    if h.startswith("@"):
        try:
            return store.parse_channel(h)
        except ValueError:
            pass
    return store.parse_channel(cand["id"])


def run(dd: str, job_id: str, cfg: Config, *, should_stop=None) -> dict:
    """Carry out a search job. Never raises for a YouTube hiccup on one channel;
    a failure that stops the whole search is recorded on the job."""
    job = load_job(dd, job_id)
    if not job:
        raise ShortForgeError(f"no search job {job_id}")
    J = _Job(dd, job)
    J.update(status="running", stage="starting", pid=os.getpid(), started=time.time())
    stop = lambda: bool(should_stop and should_stop()) or _cancelled(dd, job_id)  # noqa: E731
    try:
        _run(J, cfg, stop)
        if stop():
            J.update(status="cancelled", stage="stopped", finished=time.time())
        else:
            J.update(status="done", finished=time.time())
    except Exception as e:  # noqa: BLE001 - record it; the UI shows the reason
        log.exception("discover: search failed")
        J.update(status="failed", stage="failed", error=str(e)[:500], finished=time.time())
    return J.job


def _run(J: _Job, cfg: Config, stop) -> None:
    p = J.job["params"]
    dd = J.dd
    want = p["count"]
    country, language = p["country"], p["language"]
    ref: dict | None = None

    # ---- 1. what are we looking for? -------------------------------------- #
    if p["mode"] == "like":
        J.update(stage="reading the reference channel")
        key, url = store.parse_channel(p["reference"])
        info = _listing(store.shorts_tab_url(url), cfg, 30)
        titles = [e.get("title") or "" for e in info.get("entries") or [] if e]
        if not titles:
            raise ShortForgeError("the reference channel has no Shorts to learn from")
        about = {}
        try:
            about = fetch_about(url, cfg)
        except ShortForgeError as e:
            J.note(f"couldn't read the reference's About page ({e}) — its country is unknown")
        ref_lang, _ = LG.detect(titles)
        ref_cc = CO.code_for(about.get("country", ""))
        ref = {"key": key, "url": url, "id": info.get("channel_id") or "",
               "name": info.get("channel") or info.get("title") or key,
               "handle": info.get("uploader_id") or "", "titles": titles,
               "description": about.get("description", ""),
               "language": ref_lang, "country_code": ref_cc,
               "subscribers": info.get("channel_follower_count")}
        if language == "same":
            language = ref_lang
        if country == "same":
            country = ref_cc
            if not ref_cc:
                J.note("the reference doesn't list its country, so any country is accepted")
        name_bits = set(tokens(ref["name"] + " " + ref["handle"]))
        tag_counts = hashtags(titles + [ref["description"]])
        tags = [t for t, _ in tag_counts.most_common(12)
                if not any(b in t for b in name_bits if len(b) > 3)][:5]
        word_counts = Counter(w for t in titles for w in tokens(t) if w not in name_bits)
        keywords = [w for w, n in word_counts.most_common(10) if n >= 2] or \
                   [w for w, _ in word_counts.most_common(6)]
        want_text = (f"Channels like '{ref['name']}'. Their Shorts are titled: "
                     + " | ".join(titles[:12])
                     + (f". About: {ref['description'][:300]}" if ref["description"] else ""))
        if p["prompt"]:                      # "like this one, but …"
            want_text += f"\nThe user adds: {p['prompt']}"
            keywords = list(dict.fromkeys(tokens(p["prompt"])[:6] + keywords))
        J.update(reference={k: ref[k] for k in ("name", "url", "language", "country_code",
                                                "subscribers")})
    else:
        language = "" if language == "same" else language
        country = "" if country == "same" else country
        keywords = tokens(p["prompt"])[:10]
        tags = []
        want_text = p["prompt"]

    if language == "hi-Latn":
        lang_ok = lambda d: d in ("hi-Latn", "hi", "ur")  # noqa: E731
    else:
        lang_ok = lambda d: LG.matches(d, language)       # noqa: E731
    J.update(effective={"country": country, "language": language,
                        "country_name": CO.name(country) if country else "any",
                        "language_name": LG.label(language) if language else "any"})

    queries = plan_queries(want_text if p["mode"] == "describe" else "", keywords,
                           tags, country, language, from_reference=p["mode"] == "like")
    ai_on = p["use_ai"] and ai_available()
    if p["use_ai"] and not ai_on:
        J.note("AI is switched on but no AI model is set up (Settings → Task routing → "
               "Shorts channel finder) — matching by keywords only")
    if ai_on:
        J.update(stage="asking the AI what to search for")
        try:
            plan = ai_plan(want_text, country, language)
            queries = plan["queries"] + [q for q in queries if q not in plan["queries"]]
            keywords = list(dict.fromkeys(keywords + plan["keywords"]))
            if plan["summary"]:
                want_text = plan["summary"] + "\n" + want_text
        except Exception as e:  # noqa: BLE001 - fall back, and say so
            J.note(f"the AI couldn't plan the search ({str(e)[:120]}) — using keywords instead")
            ai_on = False
    queries = queries[:10]
    if not queries:
        raise ShortForgeError("nothing to search for — add a few words to the description")
    J.update(queries=queries)

    # ---- 2. collect candidates -------------------------------------------- #
    saved_keys, saved_ids = _known(dd)
    skip_ids = saved_ids | dismissed(dd) | set(p.get("exclude_ids") or [])
    if ref and ref.get("id"):
        skip_ids.add(ref["id"])
    cands: dict[str, dict] = {}
    checked_ids: set[str] = set()
    hl = _hl_for(language)

    def add(found: list[dict], source: str, weight: float) -> None:
        for rank, c in enumerate(found):
            cid = c["id"]
            if cid in skip_ids:
                continue
            h = (c.get("handle") or "").lower()
            if h and h in saved_keys:
                continue
            cur = cands.get(cid)
            if cur is None:
                cur = cands[cid] = {**c, "hits": 0.0, "sources": []}
            for k in ("subscribers", "description", "avatar", "handle", "name"):
                if not cur.get(k) and c.get(k):
                    cur[k] = c[k]
            cur["hits"] += weight / (1 + rank / 10)
            if source not in cur["sources"]:
                cur["sources"].append(source)

    per_query = 30 if want <= 15 else 45

    def run_searches(qs: list[str]) -> None:
        for i, q in enumerate(qs):
            if stop():
                return
            J.update(stage=f"searching YouTube: “{q}”",
                     progress={"queries_done": i, "queries_total": len(qs),
                               "candidates": len(cands)})
            for fn, weight in ((search_channels, 1.0), (search_video_channels, 0.6)):
                try:
                    add(fn(q, cfg, per_query, lang=hl), f"search: {q}", weight)
                except ShortForgeError as e:
                    if "not a bot" in str(e).lower() or "requiring authentication" in str(e).lower():
                        raise
                    log.info("discover: search '%s' failed: %s", q, str(e)[:120])

    if ref:
        J.update(stage="looking at the channels the reference features")
        add(featured_channels(ref["url"], cfg), "featured by the reference", 2.0)
    run_searches(queries)
    if stop():
        return
    if not cands:
        J.note("YouTube returned no channels for these searches")
        return

    def prior(c):
        k, _ = keyword_score(c, keywords)
        return (c["hits"] + 0.8 * k, math.log10((c.get("subscribers") or 0) + 10))

    # ---- 3. check each candidate ------------------------------------------ #
    need_about = bool(country)
    # Titles of every channel confirmed to be in the wanted language — even ones
    # turned down later for country or size: their words feed the next searches.
    # Each entry: (titles, number of topic words it matched, detected language).
    in_language: dict[str, tuple[list[str], int, str]] = {}
    if ref and language and lang_ok(ref.get("language") or ""):
        # on-topic by definition
        in_language[ref["id"] or "ref"] = (ref["titles"], 99, ref.get("language") or "")

    def check(c: dict) -> dict | None:
        try:
            info = _listing(store.shorts_tab_url(c["url"]), cfg, SAMPLE)
        except ShortForgeError as e:
            J.reject("no_shorts" if "no shorts" in str(e).lower() else "unreachable")
            return None
        entries = [e for e in info.get("entries") or [] if e]
        c["titles"] = [e.get("title") or "" for e in entries]
        c["shorts_seen"] = len(entries)
        c["views"] = sorted([e.get("view_count") or 0 for e in entries])[len(entries) // 2] \
            if entries else 0
        c["subscribers"] = info.get("channel_follower_count") or c.get("subscribers")
        c["name"] = info.get("channel") or c.get("name")
        c["handle"] = info.get("uploader_id") or c.get("handle")
        if not entries:
            J.reject("no_shorts")
            return None
        if len(entries) < min(p["min_shorts"], SAMPLE):
            J.reject("too_few_shorts")          # the whole tab is shorter than asked
            return None
        lang, conf = LG.detect(c["titles"] + [c.get("description", "")])
        c["language"], c["language_conf"] = lang, conf
        if language and lang and not lang_ok(lang):
            J.reject("wrong_language")
            return None
        if language and lang:
            in_language[c["id"]] = (c["titles"], keyword_score(c, keywords)[0], lang)
        if p["min_subscribers"] and (c.get("subscribers") or 0) < p["min_subscribers"]:
            J.reject("too_small")
            return None
        if language and not lang and not ai_on:
            J.reject("unknown_language")
            return None
        if need_about:
            try:
                about = fetch_about(c["url"], cfg)
            except ShortForgeError:
                J.reject("unreachable")
                return None
            c["about"] = about
            cc = CO.code_for(about.get("country", ""))
            c["country_code"], c["country"] = cc, about.get("country", "")
            if cc and cc != country:
                J.reject("wrong_country")
                return None
            if not cc and not p["allow_unlisted_country"]:
                J.reject("no_country")
                return None
            if about.get("description"):
                c["description"] = about["description"]
        if p["min_shorts"] > SAMPLE:
            # Counting a channel's Shorts means paging through its whole Shorts
            # tab, so it is done last — only for channels that passed everything
            # else — and stops as soon as the minimum is reached.
            try:
                n = count_shorts(c["url"], cfg, p["min_shorts"])
            except ShortForgeError:
                J.reject("unreachable")
                return None
            c["shorts_seen"] = n
            if n < p["min_shorts"]:
                J.reject("too_few_shorts")
                return None
        return c

    accepted: list[dict] = []
    reserve: list[dict] = []          # right in every way, but no country listed
    checked = 0
    pool = ThreadPoolExecutor(max_workers=4)

    def verify(budget: int) -> None:
        nonlocal checked, ai_on
        queue = sorted((c for c in cands.values() if c["id"] not in checked_ids),
                       key=prior, reverse=True)[:budget]
        pos = 0
        while pos < len(queue) and len(accepted) < want and not stop():
            short = want - len(accepted)
            batch = queue[pos:pos + max(6, min(16, short * 2))]
            pos += len(batch)
            checked_ids.update(c["id"] for c in batch)
            J.update(stage=f"checking channels ({checked} checked, {len(accepted)} of {want} found)")
            passed = [r for r in pool.map(check, batch) if r]
            checked += len(batch)
            if passed and ai_on:
                try:
                    verdicts = ai_judge(want_text, language, passed)
                except Exception as e:  # noqa: BLE001 - keep going without the AI
                    J.note(f"the AI couldn't judge channels ({str(e)[:120]}) — keyword "
                           f"matching for the rest")
                    ai_on = False
                    verdicts = {}
                kept = []
                for c in passed:
                    v = verdicts.get(c["id"])
                    if v is None:
                        kept.append(c)
                        continue
                    c["fit"], c["why"] = v["fit"], v["why"]
                    if language and not c.get("language") and v.get("language"):
                        c["language"] = v["language"]
                        if not lang_ok(c["language"]):
                            J.reject("wrong_language")
                            continue
                    if v["fit"] < 5:
                        J.reject("not_a_match")
                        continue
                    kept.append(c)
                passed = kept
            for c in passed:
                if language and not c.get("language"):
                    J.reject("unknown_language")
                    continue
                n, hit = keyword_score(c, keywords)
                c["matched"] = hit
                if c.get("fit") is None:
                    c["fit"] = None
                    c["why"] = ("matches: " + ", ".join(hit[:4])) if hit else \
                        "found by searching " + c["sources"][0].replace("search: ", "")
                if country and not c.get("country_code"):
                    reserve.append(c)
                else:
                    accepted.append(c)
                if len(accepted) >= want:
                    break
            J.update(results=[_result(c, country) for c in accepted],
                     progress={"checked": checked, "accepted": len(accepted),
                               "reserve": len(reserve), "target": want,
                               "candidates": len(cands)})

    try:
        verify(max(40, want * 12))
        # ---- 3b. not enough in the wanted language: learn its words, search again
        used = list(queries)
        for _round in range(2):
            if len(accepted) >= want or stop() or not language or language == "en":
                break
            # Learn only from channels that are on-topic as well as in the right
            # language when there are any — an off-topic channel's words drag
            # the next search off-topic (seen live: a cooking search drifting to
            # "explore laugh" and a comedy creator).
            on_topic = {k: t for k, (t, n, _) in in_language.items() if n > 0}
            learned = learned_queries(on_topic or {k: t for k, (t, _, _) in in_language.items()},
                                      language, used,
                                      detected={k: d for k, (_, _, d) in in_language.items()})
            if not learned:
                J.note(f"{'no channel' if not in_language else 'too few channels'} in "
                       f"{LG.label(language)} turned up to learn search words from — "
                       f"describing the channels IN {LG.label(language)} (or setting up "
                       f"an AI model) helps a lot")
                break
            J.note("searching again with words learned from channels found in "
                   f"{LG.label(language)}: " + ", ".join(f"“{q}”" for q in learned))
            used += learned
            J.update(queries=used)
            run_searches(learned)
            verify(max(30, (want - len(accepted)) * 10))

        # Fill up with channels that don't list a country (only if allowed).
        if len(accepted) < want and reserve:
            take = reserve[:want - len(accepted)]
            accepted.extend(take)
            J.note(f"{len(take)} result(s) don't list their country on YouTube — they "
                   f"matched everything else and are marked “not listed”")

        # Country for display, for those not already read.
        missing = [c for c in accepted if "about" not in c]
        if missing and not stop():
            J.update(stage="reading the found channels' About pages")

            def about_only(c):
                try:
                    a = fetch_about(c["url"], cfg)
                    c["about"] = a
                    c["country_code"] = CO.code_for(a.get("country", ""))
                    c["country"] = a.get("country", "")
                    if a.get("description") and not c.get("description"):
                        c["description"] = a["description"]
                except ShortForgeError:
                    pass
            list(pool.map(about_only, missing))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Country confirmed first, then the AI's fit, then topic words matched — but
    # only up to 2: past that a channel is clearly on-topic, and a name that
    # happens to repeat the search words shouldn't lift a 200-subscriber channel
    # over a million-subscriber one (seen live: "Pakistani Street Food", 214
    # subscribers, ranked above "Street Food PK", 1.44M).
    accepted.sort(key=lambda c: (bool(c.get("country_code")) or not country,
                                 c.get("fit") if c.get("fit") is not None else 5,
                                 min(len(c.get("matched") or []), 2),
                                 c.get("subscribers") or 0), reverse=True)
    results = [_result(c, country) for c in accepted[:want]]
    found = len(results)
    stage = (f"found {found} of {want}" if found < want else f"found all {want}")
    J.update(results=results, stage=stage,
             progress={"checked": checked, "accepted": found, "target": want,
                       "candidates": len(cands)})
    if found < want:
        rej = J.job.get("rejected") or {}
        top = ", ".join(f"{n} {REASONS.get(r, r)}" for r, n in
                        sorted(rej.items(), key=lambda kv: -kv[1])[:4])
        J.note(f"only {found} of {want} channels fit — {checked} were checked"
               + (f"; turned down: {top}" if top else "")
               + ". Loosen the language/country/minimum filters, describe it differently, "
                 "or press “Find more”.")


def _result(c: dict, country: str) -> dict:
    try:
        key, url = _key_for(c)
    except ValueError:
        key, url = f"channel/{c['id']}", c["url"]
    cc = c.get("country_code") or ""
    return {
        "channel_id": c["id"], "key": key, "url": url, "name": c.get("name") or key,
        "handle": c.get("handle") or "", "subscribers": c.get("subscribers"),
        "shorts_seen": c.get("shorts_seen"), "median_views": c.get("views"),
        "country_code": cc, "country": CO.name(cc) if cc else (c.get("country") or ""),
        "country_status": ("match" if country and cc == country else
                           "not listed" if not cc else "shown" if not country else "other"),
        "language": c.get("language") or "", "language_label": LG.label(c.get("language") or ""),
        "fit": c.get("fit"), "why": c.get("why") or "",
        "sample_titles": [t for t in (c.get("titles") or []) if t][:3],
        "avatar": c.get("avatar") or "", "sources": c.get("sources", [])[:3],
    }


def add_to_wishlist(dd: str, results: list[dict], count: int, *, rights_confirmed: bool,
                    query_label: str = "") -> tuple[list[dict], list[str]]:
    """Put found channels on the download wishlist (not your main channels)."""
    text = "\n".join(r["key"] if r["key"].startswith("@") else r["url"] for r in results)
    meta = {}
    for r in results:
        try:
            k = store.parse_channel(r["key"] if r["key"].startswith("@") else r["url"])[0]
        except ValueError:
            continue
        meta[k] = {"name": r.get("name"), "channel_id": r.get("channel_id"),
                   "subscribers": r.get("subscribers"), "country": r.get("country"),
                   "language": r.get("language_label"), "found_by": query_label[:120]}
    return store.add_channels(dd, text, count, rights_confirmed=rights_confirmed,
                              list_name=store.WISHLIST, meta=meta)
