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
_ASPECT_WORDS = {"portrait": "9:16", "landscape": "16:9", "square": "1:1"}
_RESOLUTIONS = {"1080p", "720p", "480p", "360p"}

HELP = (
    "<b>ShortForge</b> — send me links, I'll cut the shorts.\n\n"
    "<b>Add links</b> (settings are optional, and apply to the links in that message):\n"
    "<code>https://youtu.be/AAA clips=5 duration=120 res=720p</code>\n"
    "<code>https://youtu.be/BBB clips=6 duration=60 aspect=9:16 label=podcast</code>\n"
    "You can paste several links in one message.\n\n"
    "<b>Settings:</b> clips, duration (seconds), aspect (9:16 / 16:9 / 1:1 / portrait / "
    "landscape / square), res (1080p / 720p / 480p), lang, template, label\n\n"
    "<b>Commands:</b>\n"
    "/status — how the queue is doing\n"
    "/list — the queued links\n"
    "/clear — remove finished jobs\n"
    "/cancel — drop everything still pending\n"
    "/help — this message"
)


# --- transport -------------------------------------------------------------- #

def _call(token: str, method: str, params: dict, timeout: int = 40) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(_API.format(token=token, method=method), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _reply(token: str, chat_id: str, text: str) -> None:
    try:
        _call(token, "sendMessage",
              {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML"}, timeout=20)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram reply failed: %s", e)


# --- parsing (pure, unit-tested) -------------------------------------------- #

def parse_settings(text: str) -> dict:
    """Extract whitelisted key=value settings. Unknown keys and out-of-range
    values are dropped rather than trusted."""
    out: dict = {}
    for raw_key, raw_val in _KV_RE.findall(text):
        key = _ALIASES.get(raw_key.strip().lower())
        if not key:
            continue
        val = raw_val.strip().strip(",;")
        if key in ("num_clips", "duration", "tolerance"):
            try:
                n = int(val)
            except ValueError:
                continue
            # Bounded: refuse absurd values that would wedge the machine.
            if key == "num_clips" and not (0 < n <= 50):
                continue
            if key == "duration" and not (5 <= n <= 1800):
                continue
            if key == "tolerance" and not (1 <= n <= 120):
                continue
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
    return out


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
        msg = (f"📊 <b>Queue</b>\n⏳ {c[Q.PENDING]} pending · ▶ {c[Q.RUNNING]} running · "
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

    detail = ", ".join(f"{k}={v}" for k, v in settings.items()) or "default settings"
    return (f"➕ Queued <b>{len(links)}</b> link(s) with {detail}.\n"
            f"⏳ {Q.counts(q)[Q.PENDING]} pending — I'll start working and message you "
            f"as each one finishes.")


# --- polling loop ----------------------------------------------------------- #

def poll_once(token: str, owner_chat: str, offset: int, work_dir: str,
              timeout: int = 30) -> int:
    """One long-poll. Returns the next offset. Non-owner updates are dropped."""
    try:
        body = _call(token, "getUpdates",
                     {"offset": offset, "timeout": timeout,
                      "allowed_updates": json.dumps(["message"])},
                     timeout=timeout + 15)
    except Exception as e:  # noqa: BLE001 - network blips must not kill the listener
        log.debug("telegram poll error: %s", e)
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
