"""Command vocabulary shared by every remote-control surface (ntfy today).

One command parser, so a phone message means the same thing everywhere it's
read from — a message either a recognised ``/command`` (or its plain-English
equivalent — nobody types slash commands precisely on a phone) or a list of
http(s) links with ``key=value`` settings.

SECURITY MODEL — this is a remote-control surface on the operator's machine,
so it is deliberately narrow:

* **Fixed vocabulary.** There is no passthrough to a shell, the filesystem, or
  arbitrary config — only the whitelisted keys in ``queue.JOB_SETTINGS`` are
  accepted, and each is type-checked and bounded.
* **No secrets out.** Replies never include tokens, keys, or absolute paths.
* **Bounded.** Caps on links per message and total queue size stop a runaway
  or malicious flood from filling the disk.
* Who is even allowed to publish a command is enforced by the transport (the
  ntfy *command* topic must be a long, unguessable, ideally token-protected
  secret — see ``ntfy_bot.py``), not by this module.
"""

from __future__ import annotations

import json
import os
import re
import time

from . import queue as Q

MAX_LINKS_PER_MESSAGE = 20
MAX_QUEUE = 200

# --- shared chat log ---------------------------------------------------------- #
# ntfy and the dashboard's Chat screen are two windows onto the SAME
# conversation: a command sent from your phone must show up in the dashboard,
# and one typed in the dashboard must be something you could have sent from
# your phone. Both call log_exchange() after handle_text() so either surface
# can render the full merged history via read_chat_log().

CHAT_LOG_FILE = "chat_log.jsonl"
_CHAT_LOG_MAX = 300          # trimmed once the file grows past ~2x this


def chat_log_path(work_dir: str = ".shortforge") -> str:
    return os.path.join(work_dir, CHAT_LOG_FILE)


def log_exchange(work_dir: str, source: str, text: str, reply: str) -> None:
    """Append one (incoming, reply) exchange. Best-effort — logging the
    conversation must never be the reason a real command fails."""
    entry = {"ts": time.time(), "source": source, "text": text, "reply": reply}
    path = chat_log_path(work_dir)
    try:
        os.makedirs(work_dir, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return
    _trim_chat_log(path)


def _trim_chat_log(path: str, keep: int = _CHAT_LOG_MAX) -> None:
    """Keep the log bounded. Only pays for a rewrite once it's meaningfully
    oversized, not on every single append."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= keep * 2:
        return
    tail = lines[-keep:]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(tail)
        os.replace(tmp, path)
    except OSError:
        pass


def read_chat_log(work_dir: str = ".shortforge", limit: int = 100) -> list[dict]:
    """The last ``limit`` exchanges, oldest first. Never raises — a missing or
    corrupt log just reads as empty."""
    try:
        with open(chat_log_path(work_dir), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out

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

# Plain-English equivalents of /resume and /pause — a reply to the "should I
# start?" permission prompt is exactly the kind of message nobody wants to
# type a slash for. Exact match on the whole (stripped, lowercased) message
# only, so these never fire from a stray word inside a longer link+settings
# message.
_START_WORDS = {"start", "go", "begin", "yes", "yep", "yeah", "ok", "okay", "on"}
_STAY_PAUSED_WORDS = {"pause", "stop", "off", "no", "nope", "wait", "not yet", "hold"}
# Same idea for /cancel and /clear: an operator who already learned "pause"/
# "start" work without a slash reasonably assumes "cancel"/"clear" do too. They
# didn't — a bare "cancel" fell all the way through to "Send me a video link",
# a confusing reply that gives no hint anything was misunderstood, while
# silently doing nothing (a real report: cancelled a job from the phone, went
# to the Queue screen, the job was still running — because "cancel" alone had
# never actually reached the /cancel handler).
_CANCEL_WORDS = {"cancel", "cancel all", "cancel pending", "drop pending"}
_CLEAR_WORDS = {"clear", "clean", "clear finished", "clean up"}


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
    "/pause (or just \"pause\"/\"stop\"/\"no\") — stop starting new links "
    "(the current one finishes)\n"
    "/resume (or just \"start\"/\"go\"/\"yes\") — start working again\n"
    "/clear — remove finished jobs\n"
    "/cancel — drop everything still pending\n"
    "/help — this message"
)


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


def _pause(work_dir: str) -> str:
    q = Q.load_queue(work_dir)
    Q.set_paused(q, True)
    Q.save_queue(q, work_dir)
    return ("⏸ <b>Paused.</b> The link being worked on will finish, then I'll stop "
            "starting new ones. Links you send are still queued. Reply \"start\" "
            "(or /resume) to continue.")


def _resume(work_dir: str) -> str:
    q = Q.load_queue(work_dir)
    Q.set_paused(q, False)
    Q.save_queue(q, work_dir)
    pending = Q.counts(q)[Q.PENDING]
    return (f"▶️ <b>Working again.</b> {pending} link(s) pending."
            if pending else "▶️ <b>Working again.</b> Nothing queued — send me a link.")


def _clear(work_dir: str) -> str:
    q = Q.load_queue(work_dir)
    before = len(q.get("jobs", []))
    q["jobs"] = [j for j in q.get("jobs", []) if j["status"] in (Q.PENDING, Q.RUNNING)]
    Q.save_queue(q, work_dir)
    return f"🧹 Removed {before - len(q['jobs'])} finished job(s)."


def _cancel(work_dir: str) -> str:
    q = Q.load_queue(work_dir)
    before = len(q.get("jobs", []))
    q["jobs"] = [j for j in q.get("jobs", []) if j["status"] != Q.PENDING]
    Q.save_queue(q, work_dir)
    return f"🛑 Dropped {before - len(q['jobs'])} pending job(s). Any running job finishes."


def handle_text(text: str, work_dir: str) -> str:
    """Turn one owner message into a reply. Pure w.r.t. any transport (testable)."""
    text = (text or "").strip()
    low = text.lower()

    if low in _START_WORDS or low.startswith(("/resume", "/on")):
        return _resume(work_dir)
    if low in _STAY_PAUSED_WORDS or low.startswith(("/pause", "/stop", "/off")):
        return _pause(work_dir)
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
    if low in _CLEAR_WORDS or low.startswith("/clear"):
        return _clear(work_dir)
    if low in _CANCEL_WORDS or low.startswith("/cancel"):
        return _cancel(work_dir)
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
