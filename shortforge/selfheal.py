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

import re

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


_RULES = [
    (
        "ytdlp_stale",
        r"unable to extract|unsupported url|player response|nsig extraction|"
        r"failed to parse json|precondition check failed",
        "YouTube changed something and the downloader is out of date.",
        _update_ytdlp,
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
        lines.append(why or f"<code>{error[:400]}</code>")
    return fixed, "\n".join(lines)
