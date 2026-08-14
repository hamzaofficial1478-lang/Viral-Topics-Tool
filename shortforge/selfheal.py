"""Error triage: explain failures over ntfy, and auto-fix the safe ones.

Two deliberately separate things:

* **Automatic remedies** — a small whitelist of *operational* repairs that are
  safe to run unattended (update a stale extractor, switch download auth, free
  cache space). These change the machine's state, never the source code, and
  each is idempotent so a retry loop can't do damage.
* **AI diagnosis** — anything not on the whitelist is described in plain
  language by the configured LLM and sent over ntfy for the operator to act
  on. The model never edits files: an unreviewed code change on a production
  machine can silently break a working pipeline, and nobody is awake to catch it.
"""

from __future__ import annotations

import json
import os
import re
import time

from .utils import log

# (name, matcher, human summary, remedy callable or None)
# Ordered: the first match wins, so put specific patterns before general ones.


def _update_ytdlp() -> tuple[bool, str]:
    from .ingest import update_ytdlp
    return update_ytdlp()


def _clear_cache() -> tuple[bool, str]:
    """Drop cached downloads to free space. Sources are re-fetched on demand,
    so this loses nothing but time.

    Deliberately does NOT touch ``hookscores``. Those are small hash-named JSON
    score files — clearing them frees essentially nothing, while every entry
    represents a *paid* LLM hook-scoring pass that would have to be bought
    again on the next run. An emergency disk remedy that spends money to
    reclaim kilobytes is a bad trade; the gigabytes are all in ``downloads``.
    """
    from .maintenance import clear_downloads
    from .config import Config
    work = Config.load().get("paths.work_dir", ".shortforge")
    freed = clear_downloads(work)
    return True, f"cleared ~{freed / 1e9:.1f} GB of downloaded sources"


# Plain-English name for each whitelisted remedy, so the approval request says
# what it will actually DO rather than naming an internal rule.
_REMEDY_LABELS = {
    "ytdlp_stale": "update yt-dlp to the latest version (YouTube changed something)",
    "disk_full": "clear old cached downloads to free disk space",
}

_RULES = [
    (
        "ytdlp_stale",
        r"unable to extract|unsupported url|player response|nsig extraction|"
        r"failed to parse json|precondition check failed",
        "YouTube changed something and the downloader is out of date.",
        _update_ytdlp,
    ),
    (
        "drm",
        r"drm.?protected|protected by drm|\bdrm\b",
        "This video's stream is encrypted (DRM) — it cannot be downloaded.",
        None,   # nothing repairs DRM; the link itself is the limit
    ),
    (
        "bot_wall",
        r"not a bot|sign in to confirm",
        "YouTube asked for a login on this download.",
        None,   # the download already tries every cookie strategy; needs a human
    ),
    (
        "disk_full",
        r"no space left|not enough space|errno 28|disk full",
        "The disk filled up.",
        _clear_cache,
    ),
    (
        "network",
        r"10054|forcibly closed|reset by peer|timed out|connection aborted|"
        r"temporarily unavailable|getaddrinfo",
        "A network connection dropped.",
        None,   # already retried with backoff at the call site
    ),
    (
        "demucs_missing",
        r"demucs",
        "Dubbing needs Demucs installed in this Python.",
        None,
    ),
    (
        "no_speech",
        r"no detectable speech|transcript is empty",
        "That source has no speech to cut clips from.",
        None,
    ),
]


# Where the fault actually lies. The operator's question every time a job fails
# is "is this the video, or is this the program?" — those need completely
# different responses, and lumping them together was why a failure message read
# the same whether a video was undownloadable or ShortForge had a bug.
LINK, MACHINE, PIPELINE = "link", "machine", "pipeline"

_LINK_SIGNS = (
    r"drm|private video|members-only|removed|deleted|does not exist|is not available|"
    r"unavailable|not a bot|sign in to confirm|age.?restricted|no video formats|"
    r"requested format is not available|no detectable speech|transcript is empty|"
    r"copyright|blocked in your country|404"
)
_MACHINE_SIGNS = (
    r"no space left|not enough space|errno 28|disk full|not found on path|"
    r"is not installed|no such file or directory: 'ffmpeg|winerror 2|"
    r"10054|forcibly closed|reset by peer|timed out|connection aborted|"
    r"temporarily unavailable|getaddrinfo|unable to extract|player response|"
    r"nsig extraction|memory|cuda|out of memory"
)


def origin(error: str) -> str:
    """Is this the LINK's fault, the MACHINE's, or ShortForge's own code?

    * ``LINK`` — this particular video cannot be processed (DRM, private,
      removed, no speech). Nothing to repair; the program is working correctly.
    * ``MACHINE`` — the environment needs attention (disk full, ffmpeg missing,
      network dropped, extractor out of date). Repairable, and safe to repair.
    * ``PIPELINE`` — an unrecognised failure, i.e. most likely a bug in
      ShortForge itself. **Never** auto-repaired: changing the program's own
      behaviour unattended is the one thing the operator asked to stay in
      their hands, and a wrong "fix" to working code is far more expensive
      than a failed job.
    """
    low = (error or "").lower()
    if re.search(_LINK_SIGNS, low):
        return LINK
    if re.search(_MACHINE_SIGNS, low):
        return MACHINE
    return PIPELINE


def classify(error: str) -> tuple[str, str, object]:
    """(kind, human summary, remedy|None) for an error message."""
    low = (error or "").lower()
    for kind, pattern, summary, remedy in _RULES:
        if re.search(pattern, low):
            return kind, summary, remedy
    return "unknown", "Something unexpected went wrong.", None


def attempt_fix(error: str) -> tuple[bool, str]:
    """Run the whitelisted remedy for this error, if one exists.

    Returns (fixed, detail). ``fixed=True`` means the caller may retry the job.
    """
    kind, summary, remedy = classify(error)
    if remedy is None:
        return False, summary
    log.info("self-heal: '%s' matched — applying automatic remedy", kind)
    try:
        ok, detail = remedy()
    except Exception as e:  # noqa: BLE001 - a failed repair must not mask the original error
        return False, f"{summary} Automatic repair failed: {e}"
    return bool(ok), f"{summary} {detail}"


def diagnose(error: str, context: str = "") -> str:
    """Ask the configured LLM to explain a failure in plain language.

    Read-only: it returns text for the operator. Returns "" when no LLM is
    configured, so this is always optional.
    """
    try:
        from .providers import call_task_chat
        from .providers.store import load_store
        prompt = (
            "A video-processing pipeline failed. In at most 4 short lines, plain "
            "English, no code blocks: (1) what went wrong, (2) the single most "
            "likely cause, (3) the one action the operator should take. Be concrete.\n\n"
            f"Context: {context[:300]}\nError: {error[:1200]}"
        )
        content, _model, _fails = call_task_chat(
            load_store(), "metadata", [{"role": "user", "content": prompt}],
            max_tokens=250, timeout=60, retries=0)
        return (content or "").strip()[:900]
    except Exception as e:  # noqa: BLE001 - diagnosis is a nicety, never a blocker
        log.debug("diagnosis unavailable: %s", e)
        return ""


PENDING_FIX_FILE = "pending_fix.json"


def _pending_path(work_dir: str) -> str:
    return os.path.join(work_dir, PENDING_FIX_FILE)


def ask_first() -> bool:
    """Should a repair wait for the operator's go-ahead? Default yes.

    The operator asked for an agent that "asks me if I allow the agent to fix
    that error" — so proposing beats acting, even for the whitelisted remedies
    that used to run unattended. Switchable in Settings for anyone who wants
    the old hands-off behaviour.
    """
    try:
        from .providers.store import load_store
        return bool((load_store().get("selfheal", {}) or {}).get("ask_first", True))
    except Exception:  # noqa: BLE001
        return True


def where_and_what(error: str, url: str = "") -> str:
    """The part the operator actually reads: whose fault, and what to do.

    Every failure used to look the same, so "this video is DRM-protected" and
    "ShortForge has a bug" were indistinguishable at a glance — and the
    operator had no way to tell whether to skip the link or look at the code.
    """
    place = origin(error)
    if place == LINK:
        return ("📼 <b>The problem is this video, not the program.</b>\n"
                "ShortForge is working correctly — this particular link can't be "
                "processed (DRM, private/removed, age-restricted, or no speech).\n"
                "👉 Nothing to fix. Skip it and send a different link.")
    if place == MACHINE:
        return ("🖥️ <b>The problem is this machine's setup, not the program's logic.</b>\n"
                "Something in the environment needs attention — disk space, a missing "
                "tool, the network, or an out-of-date downloader.\n"
                "👉 This is the kind of thing I can repair safely, if you allow it.")
    return ("🧩 <b>The problem looks like it's inside ShortForge itself.</b>\n"
            "This failure doesn't match any known video or machine problem, so it is "
            "most likely a bug in the pipeline code.\n"
            "👉 I will <b>not</b> change the program on my own. Reply <b>explain</b> and "
            "I'll have the agent analyse it in detail for you to review, or fix it "
            "yourself — the exact error is above.")


def propose(error: str, job_id: str, url: str, work_dir: str) -> str:
    """Record a repair this failure would allow, for the operator to approve.

    Returns a human description of what is being offered, or "" when there is
    nothing safe to offer — a DRM block (nothing to retry) or, crucially, a
    suspected bug in ShortForge itself, which is never repaired automatically
    no matter how confident any model is about the cause.
    """
    if origin(error) != MACHINE:
        return ""
    kind, summary, remedy = classify(error)
    if remedy is None:
        return ""
    entry = {"kind": kind, "summary": summary, "job_id": job_id, "url": url,
             "error": (error or "")[:500], "ts": time.time()}
    try:
        from .utils import write_json_atomic
        write_json_atomic(_pending_path(work_dir), entry)
    except OSError as e:
        log.debug("could not record the proposed fix: %s", e)
        return ""
    return _REMEDY_LABELS.get(kind, f"apply the '{kind}' remedy")


def pending(work_dir: str) -> dict | None:
    """The repair awaiting approval, if any."""
    try:
        with open(_pending_path(work_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def discard_pending(work_dir: str) -> None:
    try:
        os.remove(_pending_path(work_dir))
    except OSError:
        pass


def apply_pending(work_dir: str) -> tuple[bool, str]:
    """Run the repair the operator just approved. Returns (ok, detail).

    The remedy itself is still only ever chosen from the same whitelist — the
    operator is approving one of a fixed set of machine-state repairs, not
    granting the agent freedom to do whatever it reasons is best. Deliberate:
    an unreviewed change on a machine nobody is watching can break a working
    pipeline silently, so the agent's reach stays bounded no matter how
    capable the model behind the diagnosis is.
    """
    entry = pending(work_dir)
    if not entry:
        return False, "There's no repair waiting for approval."
    _kind, _summary, remedy = classify(entry.get("error", ""))
    if remedy is None:
        discard_pending(work_dir)
        return False, "That failure has no safe automatic repair."
    try:
        ok, detail = remedy()
    except Exception as e:  # noqa: BLE001 - a failed repair must not mask the original
        discard_pending(work_dir)
        return False, f"The repair failed: {e}"
    discard_pending(work_dir)
    return bool(ok), detail


def report(error: str, context: str = "", *, try_fix: bool = True) -> tuple[bool, str]:
    """Triage a failure: attempt a safe repair, then build an ntfy message.

    Returns (fixed, message). ``fixed=True`` means a remedy ran successfully and
    the caller may retry.
    """
    fixed, detail = (attempt_fix(error) if try_fix else (False, classify(error)[1]))
    head = "🔧 <b>Fixed automatically</b>" if fixed else "⚠️ <b>Job failed</b>"
    lines = [head, detail]
    if context:
        lines.append(f"<i>{context[:120]}</i>")
    if fixed:
        lines.append("Retrying this link now.")
    else:
        why = diagnose(error, context)
        if why:
            # The LLM's reading is a GUESS, and it was previously the only
            # account the operator got — the raw error was discarded whenever a
            # diagnosis came back. That matters: a confident "this video is
            # DRM-protected, don't retry" over what was really a bot wall or a
            # format problem sends the operator to abandon a link that would
            # have worked. Show both, labelled, so the interpretation can be
            # checked against the evidence.
            lines.append(why)
            lines.append(f"<i>Actual error:</i> <code>{error[:300]}</code>")
        else:
            lines.append(f"<code>{error[:400]}</code>")
    return fixed, "\n".join(lines)
