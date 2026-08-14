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
    # Per-writer temp name: the dashboard and the ntfy poller both trim this
    # file, and a shared "<path>.tmp" lets one rename the other's partial write
    # into place.
    import tempfile
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                   prefix=os.path.basename(path) + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
    "metadata": "metadata", "seo": "metadata", "title": "metadata",
    "titles": "metadata", "description": "metadata", "descriptions": "metadata",
    "thumbnail": "thumbnail", "thumb": "thumbnail", "cover": "thumbnail",
}

# "yes"/"on"/"true"/"1" -> True, and the negatives -> False.
_TRUTHY = {"1", "true", "yes", "y", "on", "please"}
_FALSY = {"0", "false", "no", "n", "off", "none"}

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
_START_WORDS = {
    "start", "go", "begin", "yes", "yep", "yeah", "ok", "okay", "on",
    # Natural phrasings the operator actually sent. "resume" (the plain-word
    # twin of /resume) was missing entirely, so a perfectly reasonable reply
    # to the permission prompt did nothing at all and answered "Send me a
    # video link" — no hint that the queue was sitting paused.
    "resume", "continue", "proceed", "run", "run it", "do it", "start it",
    "start now", "start work", "start working", "start the process",
    "start the task", "start processing", "go ahead", "yes please",
    "carry on", "keep going", "begin the task",
}
_STAY_PAUSED_WORDS = {
    "pause", "stop", "off", "no", "nope", "wait", "not yet", "hold",
    "hold on", "pause it", "stop it", "stop working", "wait a bit",
    "not now", "later",
}

# Trailing/leading punctuation people type without thinking ("start." / "go!").
_EDGE_PUNCT = " \t\r\n.!?,;:'\"“”‘’()[]"


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace and shed edge punctuation.

    Only ever used for whole-message command matching, and only for messages
    that contain no link at all — so a loosened match here can never swallow
    a real link+settings message.
    """
    return re.sub(r"\s+", " ", (text or "").strip().strip(_EDGE_PUNCT)).strip().lower()
# Same idea for /cancel and /clear: an operator who already learned "pause"/
# "start" work without a slash reasonably assumes "cancel"/"clear" do too. They
# didn't — a bare "cancel" fell all the way through to "Send me a video link",
# a confusing reply that gives no hint anything was misunderstood, while
# silently doing nothing (a real report: cancelled a job from the phone, went
# to the Queue screen, the job was still running — because "cancel" alone had
# never actually reached the /cancel handler).
_CANCEL_WORDS = {"cancel", "cancel all", "cancel pending", "drop pending"}
# Approving (or declining) the repair the agent offered after a failure.
_FIX_WORDS = {"fix", "fix it", "yes fix", "apply fix", "repair", "go fix",
              "try the fix", "yes please fix"}
_SKIP_WORDS = {"skip", "skip it", "no fix", "leave it", "don't fix", "dont fix"}
# For a suspected bug in ShortForge itself: analysis for the operator to read,
# never an automatic change to the program.
_EXPLAIN_WORDS = {"explain", "explain it", "why", "diagnose", "analyse", "analyze",
                  "what happened", "details"}
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
    "<b>Commands</b> — the slash is optional, both forms do the same thing:\n"
    "<b>start</b> (or /start, /resume, \"go\", \"yes\", \"start working\") — begin "
    "working through the queue\n"
    "<b>pause</b> (or /pause, \"stop\", \"no\", \"wait\") — stop starting new links "
    "(the current one finishes)\n"
    "<b>cancel</b> (or /cancel) — drop everything still pending\n"
    "<b>clear</b> (or /clear) — remove finished jobs\n"
    "<b>fix</b> — let the agent apply the repair it offered after a failure "
    "(<b>skip</b> declines)\n"
    "/status — how the queue is doing (and whether anything is stuck)\n"
    "/list — the queued links\n"
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
        elif key in ("metadata", "thumbnail"):
            v = val.strip().lower()
            if v in _TRUTHY:
                out[key] = True
            elif v in _FALSY:
                out[key] = False
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

    # "3 clips with title and description" — the way it actually gets typed.
    low_t = t.lower()
    if "metadata" not in out and re.search(
            r"\b(title|titles|description|descriptions|seo|metadata)\b", low_t):
        out["metadata"] = not re.search(
            r"\bno\s+(title|description|seo|metadata)\b", low_t)
    if "thumbnail" not in out and re.search(r"\b(thumbnail|thumb|cover)\b", low_t):
        out["thumbnail"] = not re.search(r"\bno\s+(thumbnail|thumb|cover)\b", low_t)

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


def parse_link_groups(text: str) -> list[tuple[list[str], dict]]:
    """Split a message into (links, settings) groups — one group per LINE.

    Settings used to be parsed from the WHOLE message and applied to every link
    in it, which quietly mangled the obvious way to send a batch::

        https://youtu.be/A 3 clips of 60s
        https://youtu.be/B 6 clips of 2min portrait
        https://youtu.be/C 2 clips 720p

    Every link came out as "3 clips of 1m00s, 9:16, 720p" — line 2's portrait
    and line 3's 720p leaked onto line 1, and B's "6 clips of 2min" vanished.
    Not merely a missing feature: it silently rendered something the operator
    never asked for.

    Now each line carries its own settings. A line with no links is treated as
    a default for the lines beneath it, so a shared header still works::

        3 clips of 60s portrait      <- applies to any link that says nothing
        https://youtu.be/A
        https://youtu.be/B 6 clips   <- overrides the count, keeps the rest
    """
    defaults: dict = {}
    groups: list[tuple[list[str], dict]] = []
    for line in (text or "").splitlines():
        links = parse_links(line)
        settings = parse_settings(line)
        if not links:
            defaults.update(settings)        # a bare settings line: use below
            continue
        merged = {**defaults, **settings}    # the line's own wins
        groups.append((links, merged))
    if not groups:                           # no line held a link
        return []
    return groups


def _pause(work_dir: str) -> str:
    Q.mutate_queue(work_dir, lambda q: Q.set_paused(q, True))
    return ("⏸ <b>Paused.</b> The link being worked on will finish, then I'll stop "
            "starting new ones. Links you send are still queued. Reply \"start\" "
            "(or /resume) to continue.")


def _resume(work_dir: str) -> str:
    q, _ = Q.mutate_queue(work_dir, lambda qq: Q.set_paused(qq, False))
    pending = Q.counts(q)[Q.PENDING]
    return (f"▶️ <b>Working again.</b> {pending} link(s) pending."
            if pending else "▶️ <b>Working again.</b> Nothing queued — send me a link.")


def _clear(work_dir: str) -> str:
    def _drop(q):
        before = len(q.get("jobs", []))
        q["jobs"] = [j for j in q.get("jobs", []) if j["status"] in (Q.PENDING, Q.RUNNING)]
        return before - len(q["jobs"])

    _, removed = Q.mutate_queue(work_dir, _drop)
    return f"🧹 Removed {removed} finished job(s)."


def _cancel(work_dir: str) -> str:
    def _drop(q):
        before = len(q.get("jobs", []))
        q["jobs"] = [j for j in q.get("jobs", []) if j["status"] != Q.PENDING]
        return before - len(q["jobs"])

    _, dropped = Q.mutate_queue(work_dir, _drop)
    return f"🛑 Dropped {dropped} pending job(s). Any running job finishes."


def _apply_fix(work_dir: str) -> str:
    """Approve the repair the agent offered, then re-queue the link it failed on.

    The agent proposes; the operator decides. Only remedies from selfheal's
    fixed whitelist are ever on offer — approving one is not a licence for the
    agent to do whatever it judges best on this machine.
    """
    from . import selfheal

    entry = selfheal.pending(work_dir)
    if not entry:
        return ("Nothing is waiting to be fixed. I only offer a repair right after "
                "a job fails with something I know how to address.")
    ok, detail = selfheal.apply_pending(work_dir)
    if not ok:
        return f"⚠️ <b>The repair didn't work.</b> {detail}"

    job_id = entry.get("job_id")

    def _requeue(q):
        job = Q.get_job(q, job_id) if job_id else None
        if job is None or job["status"] != Q.FAILED:
            return False
        job["status"] = Q.PENDING
        job["error"] = None
        return True

    _, requeued = Q.mutate_queue(work_dir, _requeue)
    tail = ("\n▶️ Put that link back in the queue — I'll retry it."
            if requeued else "\n(The link is no longer in the queue to retry.)")
    return f"🔧 <b>Fixed.</b> {detail}{tail}"


def _explain_last_failure(work_dir: str) -> str:
    """Have the agent analyse the most recent failure — read-only.

    For the case the operator drew a line around: a fault that looks like it is
    in ShortForge's own code. They get the analysis to review and decide on;
    the agent does not touch the program. Point Settings → Task routing at a
    stronger model and this is where its reasoning shows up.
    """
    from . import selfheal

    q = Q.load_queue(work_dir)
    failed = [j for j in q.get("jobs", []) if j["status"] == Q.FAILED and j.get("error")]
    if not failed:
        return "No failed link to explain — nothing has gone wrong recently."
    job = failed[-1]
    err = job.get("error") or ""
    place = selfheal.origin(err)
    why = selfheal.diagnose(err, context=job["url"][:120])
    head = (f"🔎 <b>Analysis</b> — {job['url'][:60]}\n"
            f"{selfheal.where_and_what(err, job['url'])}\n")
    if not why:
        return (head + f"\n<code>{err[:400]}</code>\n"
                "(No LLM is configured for analysis — set one under "
                "Settings → Task routing for a fuller explanation.)")
    tail = ("\n\n<i>This is analysis only — I have not changed anything.</i>"
            if place == selfheal.PIPELINE else "")
    return f"{head}\n{why}\n\n<i>Actual error:</i> <code>{err[:300]}</code>{tail}"


def _skip_fix(work_dir: str) -> str:
    from . import selfheal
    if not selfheal.pending(work_dir):
        return "Nothing was waiting to be fixed."
    selfheal.discard_pending(work_dir)
    return "👍 Left it alone. The link stays failed; send it again if you change your mind."


def _status(work_dir: str) -> str:
    """Queue state AND why it is or isn't moving, ending in one next action.

    "/status" used to print counts and, if anything looked stuck, a fixed
    "is start_all.bat still open?" line — printed identically whether a worker
    was running or not, because it never actually checked. That is no help at
    all for the single question the operator keeps having to ask: *I added a
    link, is it going to start on its own or do I have to send something?*
    So each state now names its own cause and its own next step.
    """
    from . import lifecycle

    q = Q.load_queue(work_dir)
    c = Q.counts(q)
    paused = Q.is_paused(q)
    worker = lifecycle.worker_is_live(work_dir)
    running = next((j for j in q.get("jobs", []) if j["status"] == Q.RUNNING), None)

    msg = (f"📊 <b>Queue</b> — {'⏸ PAUSED' if paused else '▶ working'}"
           f" · worker {'🟢 running' if worker else '🔴 not running'}\n"
           f"⏳ {c[Q.PENDING]} pending · ▶ {c[Q.RUNNING]} running · "
           f"✅ {c[Q.DONE]} done · ✗ {c[Q.FAILED]} failed\n"
           f"🎬 {Q.total_clips(q)} clip(s) produced")

    if running:
        return msg + f"\n\n🎬 Now: {running['url'][:70]}\nNothing to do — it's working."
    if not c[Q.PENDING]:
        return msg + "\n\n✅ Nothing waiting. Send me a link whenever you like."

    # Links are waiting. Exactly one of these is the reason they aren't moving.
    if not worker:
        return msg + ("\n\n🔴 <b>ShortForge isn't running on the PC</b>, so nothing can "
                      "start — the queue is only a list until a worker reads it.\n"
                      "👉 Open <b>start_all.bat</b> on the PC. It picks these up by itself.")
    if paused:
        return msg + ("\n\n⏸ <b>Waiting for your go-ahead</b> — this is the permission "
                      "gate, so nothing runs behind your back after a restart.\n"
                      "👉 Reply <b>start</b>.")
    note = _lock_note(work_dir)
    if "stale" in note:
        return msg + f"\n\n⚠️ {note}\n👉 Nothing to do — it clears itself, then work resumes."
    return msg + ("\n\n▶️ Unpaused, worker running, links waiting — the next one should "
                  "begin within a few seconds.\n👉 Nothing to send. Re-check with /status "
                  "if it hasn't moved in a minute.")


def _lock_note(work_dir: str) -> str:
    """Describe the worker lock when it looks like it is in the way."""
    import os
    path = Q.lock_path(work_dir)
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return "No worker lock is held, so nothing is blocking the queue."
    if age < Q.LOCK_STALE_AFTER:
        return f"A worker is holding the queue lock (checked in {age:.0f}s ago)."
    return (f"A stale worker lock is present ({age / 60:.0f} min without a check-in) — "
            "it will be cleared automatically on the next attempt.")


def _unknown_reply(text: str, work_dir: str) -> str:
    """A dead end is the worst possible answer to a control message.

    The old reply to anything unrecognised was a flat "Send me a video link
    (or /help)" — which is what the operator got back for "start working"
    while a link sat pending in a paused queue. It neither did the thing nor
    said why, so there was no way to discover the one word that would have
    worked. Now an unrecognised message reports the real queue state and the
    exact words that act on it, making any wrong guess self-correcting.
    """
    q = Q.load_queue(work_dir)
    c = Q.counts(q)
    state = "⏸ PAUSED" if Q.is_paused(q) else "▶ working"
    hint = ('Reply <b>start</b> to begin.' if Q.is_paused(q)
            else 'Reply <b>pause</b> to hold.')
    return (f"🤔 I didn't understand “{text[:60]}”.\n"
            f"📊 Queue: {state} — {c[Q.PENDING]} pending, {c[Q.RUNNING]} running, "
            f"{c[Q.DONE]} done.\n{hint} Also: <b>cancel</b>, <b>clear</b>, "
            f"/list, /status, /help.")


def handle_text(text: str, work_dir: str) -> str:
    """Turn one owner message into a reply. Pure w.r.t. any transport (testable)."""
    text = (text or "").strip()
    low = text.lower()

    # Bare-word commands are matched ONLY on link-free messages. Checking links
    # first is what makes it safe to accept loose multi-word phrasings above:
    # a message carrying a URL can never be mistaken for a control word.
    if not _URL_RE.search(text):
        norm = _normalize(text)
        if norm in _START_WORDS:
            return _resume(work_dir)
        if norm in _STAY_PAUSED_WORDS:
            return _pause(work_dir)
        if norm in _CLEAR_WORDS:
            return _clear(work_dir)
        if norm in _CANCEL_WORDS:
            return _cancel(work_dir)
        if norm in _FIX_WORDS:
            return _apply_fix(work_dir)
        if norm in _SKIP_WORDS:
            return _skip_fix(work_dir)
        if norm in _EXPLAIN_WORDS:
            return _explain_last_failure(work_dir)

    # "/start" STARTS. It used to return the help text — a leftover of the
    # Telegram convention where /start is the bot's intro — so an operator
    # replying "/start" to the "shall I begin?" prompt got a wall of help and a
    # queue that was still paused, with nothing saying so. Whatever the history,
    # a command named start that does not start is a trap. Help is /help.
    if low.startswith(("/resume", "/on", "/start", "/go", "/run")):
        return _resume(work_dir)
    if low.startswith(("/pause", "/stop", "/off")):
        return _pause(work_dir)
    if low.startswith("/help"):
        return HELP
    if low.startswith("/status"):
        return _status(work_dir)
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
        return _clear(work_dir)
    if low.startswith("/cancel"):
        return _cancel(work_dir)
    if text.startswith("/"):
        return "Unknown command. Send /help for what I understand."

    links = parse_links(text)
    if not links:
        return _unknown_reply(text, work_dir)

    groups = parse_link_groups(text)
    if len(Q.load_queue(work_dir).get("jobs", [])) + len(links) > MAX_QUEUE:
        return f"Queue is full ({MAX_QUEUE} max). Use /clear first."

    def _add_all(qq):
        added_all, skipped_all = [], []
        for urls, settings in groups:
            settings = dict(settings)
            label = settings.pop("_label", "")
            a, s = Q.add_links(qq, urls, settings, label=label)
            added_all += a
            skipped_all += s
        return added_all, skipped_all

    # Under the queue mutex: the worker writes this same file from another
    # thread as it claims and completes jobs, and an interleaved add would
    # either vanish or roll the worker's progress back. Also means the
    # duplicate check sees the queue as it is at write time, not before.
    q, (added, skipped) = Q.mutate_queue(work_dir, _add_all)

    # Echo back what was UNDERSTOOD per link, not what was typed — that is how
    # a misread "2 min" gets caught before an hour of rendering. Listed one per
    # line now that each link can carry its own settings, so a mistake on line
    # 3 of ten is actually visible.
    if len(added) > 1:
        detail = "\n" + "\n".join(
            f"• {j['url'][:48]} — {Q.describe_settings(j)}" for j in added)
    else:
        detail = Q.describe_settings(added[0]) if added else ""
    if not added:
        return ("⚠️ <b>Nothing added — already in the queue.</b>\n"
                + Q.describe_skipped(skipped))
    # Say whether this will actually START, and if not, what to send. The reply
    # used to promise "I'll message you when each one starts and finishes"
    # unconditionally — including when the queue was paused and therefore
    # nothing was going to start at all. That single sentence is why "I added
    # the link and nothing happened" kept recurring: the confirmation read like
    # work had begun. Same rule as everywhere else — never sound successful
    # when you aren't.
    pending = Q.counts(q)[Q.PENDING]
    if Q.is_paused(q):
        next_step = ("⏸ The queue is <b>paused</b>, so this will not start yet.\n"
                     "Reply <b>start</b> when you want me to begin.")
    else:
        next_step = ("▶️ Working through them now — I'll message you as each one "
                     "starts and finishes.")
    msg = (f"➕ Queued <b>{len(added)}</b> link(s): {detail}.\n"
           f"⏳ {pending} pending. {next_step}")
    if skipped:
        msg += (f"\n\n⚠️ Skipped {len(skipped)} already in the queue:\n"
                + Q.describe_skipped(skipped))
    return msg
