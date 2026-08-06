"""Telegram control: send links + settings from your phone, the PC does the work.

SECURITY MODEL — this is a remote-control surface on the operator's machine, so
it is deliberately narrow:

* **Owner-only.** Every update is checked against the ``chat_id`` saved in
  Settings *before* the text is parsed. Anything else is counted and dropped —
  a stranger who finds the bot cannot queue jobs, read status, or learn
  anything about the machine.
* **Fixed vocabulary.** A message is either a recognised ``/command`` or a list
  of http(s) links with ``key=value`` settings. There is no passthrough to a
  shell, the filesystem, or arbitrary config — only the whitelisted keys in
  ``queue.JOB_SETTINGS`` are accepted, and each is type-checked and bounded.
* **No secrets out.** Replies never include tokens, keys, or absolute paths.
* **Bounded.** Caps on links per message and total queue size stop a runaway
  or malicious flood from filling the disk.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

from . import queue as Q
from .utils import log

_API = "https://api.telegram.org/bot{token}/{method}"

MAX_LINKS_PER_MESSAGE = 20
MAX_QUEUE = 200

_URL_RE = re.compile(r"https?://\S+")
_KV_RE = re.compile(r"\b(\w+)\s*=\s*([^\s]+)")

# Accepted setting aliases -> queue setting name. Anything else is ignored.
_ALIASES = {
    "clips": "num_clips", "num_clips": "num_clips", "n": "num_clips",
    "duration": "duration", "dur": "duration", "len": "duration", "length": "duration",
    "tolerance": "tolerance", "tol": "tolerance",
    "aspect": "aspect", "orientation": "aspect",
    "resolution": "resolution", "res": "resolution", "quality": "resolution",
    "language": "language", "lang": "language",
    "template": "caption_template", "captions": "caption_template",
    "label": "_label",
}

_ASPECTS = {"9:16", "16:9", "1:1", "4:5", "portrait", "landscape", "square"}
_ASPECT_WORDS = {"portrait": "9:16", "vertical": "9:16",
                 "landscape": "16:9", "horizontal": "16:9", "square": "1:1"}
_RESOLUTIONS = {"1080p", "720p", "480p", "360p"}

# Bounds. Refusing an absurd value beats clamping it into something the operator
# did not ask for — they'd never know the number changed.
_BOUNDS = {"num_clips": (1, 50), "duration": (5, 1800), "tolerance": (1, 120)}

_UNIT_SECONDS = {"h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
                 "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                 "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}
_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(h|hrs?|hours?|m|mins?|minutes?|s|secs?|seconds?)\b",
                     re.I)
_MMSS_RE = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")
_CLIPS_RE = re.compile(r"\b(\d{1,3})\s*(?:x\s*)?(?:clips?|shorts?|videos?)\b", re.I)
_ASPECT_RE = re.compile(r"\b(9:16|16:9|1:1|4:5|portrait|landscape|square|vertical|horizontal)\b",
                        re.I)
_RES_RE = re.compile(r"\b(1080p|720p|480p|360p)\b", re.I)


def _bounded(key: str, value) -> int | None:
    lo, hi = _BOUNDS[key]
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _parse_duration(val: str) -> int | None:
    """Seconds from '120', '2min', '90s', '1:30' — people write all four."""
    v = (val or "").strip().lower()
    m = _MMSS_RE.fullmatch(v)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = _DUR_RE.fullmatch(v)
    if m:
        return int(round(float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]))
    try:
        return int(float(v))
    except ValueError:
        return None

HELP = (
    "<b>ShortForge</b> — send me links, I'll cut the shorts.\n\n"
    "<b>Add links.</b> Write it however you like — these all work:\n"
    "<code>https://youtu.be/AAA 5 clips of 2 min landscape</code>\n"
    "<code>https://youtu.be/BBB clips=6 duration=60 aspect=9:16 label=podcast</code>\n"
    "<code>https://youtu.be/CCC 3 shorts 1:30 1080p</code>\n"
    "Settings apply to every link in that message, so send one message per "
    "setting group.\n\n"
    "<b>Settings:</b>\n"
    "• clips — how many (1–50)\n"
    "• duration — <code>120</code>, <code>2min</code>, <code>90s</code> or "
    "<code>1:30</code> (5s–30min)\n"
    "• orientation — landscape (16:9), portrait (9:16), square, 4:5\n"
    "• res — 1080p / 720p / 480p\n"
    "• lang, template, label\n\n"
    "<b>Commands:</b>\n"
    "/status — how the queue is doing\n"
    "/list — the queued links\n"
    "/pause — stop starting new links (the current one finishes)\n"
    "/resume — start working again\n"
    "/clear — remove finished jobs\n"
    "/cancel — drop everything still pending\n"
    "/help — this message"
)


# --- transport -------------------------------------------------------------- #

def _call(token: str, method: str, params: dict, timeout: int = 40) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(_API.format(token=token, method=method), data=data)
    from .netdiag import build_opener   # honours HTTPS_PROXY / the Windows proxy
    with build_opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _reply(token: str, chat_id: str, text: str) -> None:
    try:
        _call(token, "sendMessage",
              {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML"}, timeout=20)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram reply failed: %s", e)


# --- parsing (pure, unit-tested) -------------------------------------------- #

def parse_settings(text: str) -> dict:
    """Extract whitelisted settings. Unknown keys and out-of-range values are
    dropped rather than trusted.

    Two passes: exact ``key=value`` first, then a tolerant pass over what's left
    for the way the operator actually types — "5 clips of 2 min, landscape".
    """
    out: dict = {}
    for raw_key, raw_val in _KV_RE.findall(text):
        key = _ALIASES.get(raw_key.strip().lower())
        if not key:
            continue
        val = raw_val.strip().strip(",;")
        if key in ("num_clips", "duration", "tolerance"):
            n = _parse_duration(val) if key == "duration" else val
            n = _bounded(key, n)
            if n is not None:
                out[key] = n
        elif key == "aspect":
            v = val.lower()
            v = _ASPECT_WORDS.get(v, v)
            if v in _ASPECTS or re.fullmatch(r"\d{2,5}x\d{2,5}", v):
                out[key] = v
        elif key == "resolution":
            v = val.lower()
            if v in _RESOLUTIONS or re.fullmatch(r"\d{2,5}x\d{2,5}", v):
                out[key] = v
        elif key == "language":
            if re.fullmatch(r"[A-Za-z]{2,5}(-[A-Za-z]{2,5})?", val):
                out[key] = val.lower()
        elif key == "caption_template":
            if re.fullmatch(r"[A-Za-z_]{2,32}", val):
                out[key] = val.lower()
        elif key == "_label":
            out[key] = re.sub(r"[^A-Za-z0-9 _-]", "", val)[:32]
    _parse_loose(text, out)
    return out


def _parse_loose(text: str, out: dict) -> None:
    """Fill in anything not given as key=value, from plain phrasing.

    Only ever *adds* — an explicit ``duration=90`` always wins. URLs and the
    key=value pairs are stripped first so no number is ever read out of a link.
    """
    t = _KV_RE.sub(" ", _URL_RE.sub(" ", text or ""))

    # Aspect first, and consumed: "9:16" would otherwise read as 9 min 16 s.
    m = _ASPECT_RE.search(t)
    if m:
        if "aspect" not in out:
            v = m.group(1).lower()
            out["aspect"] = _ASPECT_WORDS.get(v, v)
        t = t[:m.start()] + " " + t[m.end():]
    t = _ASPECT_RE.sub(" ", t)

    m = _RES_RE.search(t)
    if m:
        out.setdefault("resolution", m.group(1).lower())
        t = t[:m.start()] + " " + t[m.end():]

    # Clips before duration, and consumed, so "5 clips of 2 min" doesn't read the
    # 5 as five seconds.
    m = _CLIPS_RE.search(t)
    if m:
        n = _bounded("num_clips", m.group(1))
        if n is not None and "num_clips" not in out:
            out["num_clips"] = n
        t = t[:m.start()] + " " + t[m.end():]

    if "duration" not in out:
        m = _MMSS_RE.search(t)
        secs = (int(m.group(1)) * 60 + int(m.group(2))) if m else None
        if secs is None:
            m = _DUR_RE.search(t)
            if m:
                secs = int(round(float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]))
        n = _bounded("duration", secs) if secs is not None else None
        if n is not None:
            out["duration"] = n


def parse_links(text: str) -> list[str]:
    """http(s) links only, de-duplicated, capped per message."""
    seen, links = set(), []
    for m in _URL_RE.findall(text or ""):
        url = m.rstrip(".,;)")
        if url in seen:
            continue
        seen.add(url)
        links.append(url)
        if len(links) >= MAX_LINKS_PER_MESSAGE:
            break
    return links


def handle_text(text: str, work_dir: str) -> str:
    """Turn one owner message into a reply. Pure w.r.t. Telegram (testable)."""
    text = (text or "").strip()
    low = text.lower()

    if low.startswith(("/start", "/help")):
        return HELP
    if low.startswith("/status"):
        q = Q.load_queue(work_dir)
        c = Q.counts(q)
        running = next((j for j in q.get("jobs", []) if j["status"] == Q.RUNNING), None)
        state = "⏸ PAUSED" if Q.is_paused(q) else "▶ working"
        msg = (f"📊 <b>Queue</b> — {state}\n"
               f"⏳ {c[Q.PENDING]} pending · ▶ {c[Q.RUNNING]} running · "
               f"✅ {c[Q.DONE]} done · ✗ {c[Q.FAILED]} failed\n"
               f"🎬 {Q.total_clips(q)} clip(s) produced")
        if running:
            msg += f"\n\nNow: {running['url'][:70]}"
        return msg
    if low.startswith(("/pause", "/stop", "/off")):
        q = Q.load_queue(work_dir)
        Q.set_paused(q, True)
        Q.save_queue(q, work_dir)
        return ("⏸ <b>Paused.</b> The link being worked on will finish, then I'll stop "
                "starting new ones. Links you send are still queued. /resume to continue.")
    if low.startswith(("/resume", "/on")):   # /start stays Telegram's "introduce yourself"
        q = Q.load_queue(work_dir)
        Q.set_paused(q, False)
        Q.save_queue(q, work_dir)
        pending = Q.counts(q)[Q.PENDING]
        return (f"▶️ <b>Working again.</b> {pending} link(s) pending."
                if pending else "▶️ <b>Working again.</b> Nothing queued — send me a link.")
    if low.startswith("/list"):
        q = Q.load_queue(work_dir)
        jobs = q.get("jobs", [])
        if not jobs:
            return "Queue is empty. Send me a link to get started."
        marks = {Q.PENDING: "⏳", Q.RUNNING: "▶", Q.DONE: "✅", Q.FAILED: "✗"}
        lines = [f"{marks.get(j['status'], '?')} {i}. {j['url'][:60]}"
                 + (f" ({len(j['clips'])} clips)" if j["clips"] else "")
                 for i, j in enumerate(jobs[:30], 1)]
        return "<b>Queue</b>\n" + "\n".join(lines)
    if low.startswith("/clear"):
        q = Q.load_queue(work_dir)
        before = len(q.get("jobs", []))
        q["jobs"] = [j for j in q.get("jobs", []) if j["status"] in (Q.PENDING, Q.RUNNING)]
        Q.save_queue(q, work_dir)
        return f"🧹 Removed {before - len(q['jobs'])} finished job(s)."
    if low.startswith("/cancel"):
        q = Q.load_queue(work_dir)
        before = len(q.get("jobs", []))
        q["jobs"] = [j for j in q.get("jobs", []) if j["status"] != Q.PENDING]
        Q.save_queue(q, work_dir)
        return f"🛑 Dropped {before - len(q['jobs'])} pending job(s). Any running job finishes."
    if text.startswith("/"):
        return "Unknown command. Send /help for what I understand."

    links = parse_links(text)
    if not links:
        return "Send me a video link (or /help)."

    settings = parse_settings(text)
    label = settings.pop("_label", "")
    q = Q.load_queue(work_dir)
    if len(q.get("jobs", [])) + len(links) > MAX_QUEUE:
        return f"Queue is full ({MAX_QUEUE} max). Use /clear first."
    for url in links:
        Q.add_job(q, url, settings, label=label)
    Q.save_queue(q, work_dir)

    # Echo back what was UNDERSTOOD, not what was typed — that is how a
    # misread "2 min" gets caught before an hour of rendering.
    detail = Q.describe_settings({"settings": settings})
    return (f"➕ Queued <b>{len(links)}</b> link(s): {detail}.\n"
            f"⏳ {Q.counts(q)[Q.PENDING]} pending — I'll message you when each one "
            f"starts and finishes.")


# --- polling loop ----------------------------------------------------------- #

def poll_once(token: str, owner_chat: str, offset: int, work_dir: str,
              timeout: int = 30, on_result=None) -> int:
    """One long-poll. Returns the next offset. Non-owner updates are dropped.

    ``on_result(ok, detail)`` (optional) is told whether the poll reached Telegram
    at all, so the caller can notice an outage — the poll itself swallows network
    errors by design, which would otherwise hide a dead connection completely.
    """
    try:
        body = _call(token, "getUpdates",
                     {"offset": offset, "timeout": timeout,
                      "allowed_updates": json.dumps(["message"])},
                     timeout=timeout + 15)
        if on_result:
            on_result(True, "")
    except Exception as e:  # noqa: BLE001 - network blips must not kill the listener
        log.debug("telegram poll error: %s", e)
        if on_result:
            on_result(False, f"{type(e).__name__}: {str(e)[:150]}")
        time.sleep(5)
        return offset

    for upd in body.get("result", []):
        offset = max(offset, int(upd.get("update_id", 0)) + 1)
        msg = upd.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        # SECURITY GATE: owner only, checked before the text is even looked at.
        if chat != str(owner_chat):
            log.warning("telegram: ignored a message from unauthorised chat %s", chat[:12])
            continue
        text = msg.get("text") or ""
        try:
            reply = handle_text(text, work_dir)
        except Exception as e:  # noqa: BLE001
            log.error("telegram handler error: %s", e)
            reply = "Something went wrong handling that. Try /help."
        _reply(token, owner_chat, reply)
    return offset
