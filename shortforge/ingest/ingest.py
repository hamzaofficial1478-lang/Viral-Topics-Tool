"""M1 — Ingestion.

Accepts a link to the operator's *own* video (or a local file they own),
downloads best-quality source with yt-dlp, records metadata, and computes a
content hash for caching.

Ownership is enforced here: this is the one hard rule of the whole tool. We do
not ingest arbitrary third-party links — the operator must pass an explicit
ownership confirmation.

Link ingestion reliability (the operator only ever uses links):
  * cookie auth — a cookies.txt file and/or the browser's own cookies
    (``--cookies-from-browser``), tried as a fallback chain so a bot-detection
    wall is worked around automatically;
  * multiple yt-dlp player clients queried per request (default: web + tv) so
    a PO-token-gated client that silently caps out at ~360p doesn't win —
    "signed in fine, no usable high-res format" is a client problem, not an
    auth problem, and cookies alone don't fix it;
  * a *friendly* message on YouTube's "confirm you're not a bot" wall that
    points at Settings → YouTube authentication (never the raw yt-dlp error);
  * a configurable read timeout (default 120s), resumable partial downloads,
    retries with exponential backoff, and a per-video-id download cache.
"""

from __future__ import annotations

import os
import time

from ..config import Config
from ..models import SourceMeta
from ..utils import ShortForgeError, content_key, ffprobe_info, log

_URL_PREFIXES = ("http://", "https://", "www.")

_BROWSERS = ("firefox", "chrome", "edge", "brave")

# Shown verbatim on a bot-detection wall — actionable, not the raw yt-dlp error.
BOT_AUTH_MESSAGE = (
    "YouTube is requiring authentication (\"Sign in to confirm you're not a bot\"). "
    "Open Settings → YouTube authentication and select the browser you're logged "
    "into YouTube with, then retry. Firefox is the most reliable on Windows. "
    "On the CLI: add --cookies-from-browser firefox."
)


def _looks_like_url(source: str) -> bool:
    return source.startswith(_URL_PREFIXES)


# --- error classification --------------------------------------------------- #

class _BotWall(Exception):
    """YouTube's 'confirm you're not a bot' wall — auth (cookies) is the fix."""


class _Unavailable(Exception):
    """The video is private/removed/invalid — retrying or cookies won't help."""


class _Transient(Exception):
    """A network hiccup — worth retrying with backoff."""


class _NoFormat(Exception):
    """Auth worked, but no stream matched the format selector — loosen it."""


class _Forbidden(Exception):
    """HTTP 403 on the media URL itself, after metadata extraction succeeded.

    YouTube handed out a download URL and then refused the fetch. That is
    almost always the *player client* the URL was minted for being rejected
    (PO-token / SABR enforcement), not an auth or format problem — so cookies
    and looser selectors, the two things the chain already tries, cannot help.
    Retrying with a different client is what actually works.
    """


class _DRMProtected(Exception):
    """The stream is encrypted. No cookie, client or format selector helps.

    Nothing recognised DRM before, so such a video fell through every auth
    strategy in turn and ended as a generic "Download failed after trying N
    auth strategy(ies)" — which told the operator nothing, cost three pointless
    retries, and left the real cause to be guessed at by an LLM.
    """


_ANSI = None


def _clean_err(e: Exception) -> str:
    """yt-dlp errors carry ANSI colour codes and an 'ERROR:' prefix — strip them
    so the Settings 'Test' panel shows a readable message."""
    global _ANSI
    import re
    if _ANSI is None:
        _ANSI = re.compile(r"\x1b\[[0-9;]*m")
    text = _ANSI.sub("", str(e)).strip()
    line = text.splitlines()[-1] if text.splitlines() else text
    return line.replace("ERROR:", "").strip()


def _classify(e: Exception) -> Exception:
    msg = str(e).lower()
    # Checked first: a DRM stream can also report "no formats", and retrying
    # that with different cookies is a guaranteed waste of time.
    if "drm" in msg or ("encrypted" in msg and "stream" in msg):
        return _DRMProtected(str(e))
    if "403" in msg and ("forbidden" in msg or "download video data" in msg):
        return _Forbidden(str(e))
    if "requested format is not available" in msg or "no video formats found" in msg:
        return _NoFormat(str(e))
    if "not a bot" in msg or "confirm you" in msg or "sign in to confirm" in msg:
        return _BotWall(str(e))
    if any(s in msg for s in ("private video", "removed", "does not exist",
                              "unavailable", "404", "no video", "unsupported url",
                              "is not available", "members-only", "account")):
        return _Unavailable(str(e))
    if any(s in msg for s in ("forcibly closed", "timed out", "timeout", "connection",
                              "temporarily", "10054", "reset by peer", "network",
                              "unreachable", "read operation", "econnreset")):
        return _Transient(str(e))
    return e


# --- auth strategies (the fallback chain) ----------------------------------- #

def _materialise_cookies(value: str | None) -> str | None:
    """Accept either a path to cookies.txt OR the pasted CONTENTS of one.

    Operators reasonably paste the file's text into a field labelled "cookies.txt";
    handing that to yt-dlp as a filename raises a baffling "[Errno 22] Invalid
    argument". Detect Netscape cookie data and write it to a real file instead."""
    if not value:
        return None
    v = value.strip()
    if os.path.isfile(v):
        return v
    looks_like_data = ("\t" in v or "# Netscape" in v or "# HTTP Cookie File" in v
                       or ".youtube.com" in v)
    if not looks_like_data:
        return v          # a path that doesn't exist yet — let yt-dlp complain clearly
    import hashlib
    import tempfile
    name = "sf_cookies_" + hashlib.sha1(v.encode("utf-8")).hexdigest()[:10] + ".txt"
    path = os.path.join(tempfile.gettempdir(), name)
    if not v.startswith("#"):
        v = "# Netscape HTTP Cookie File\n" + v
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(v if v.endswith("\n") else v + "\n")
        log.info("cookies: pasted cookie data saved to a temporary file")
        return path
    except OSError as e:
        log.warning("could not write pasted cookie data: %s", e)
        return None


def _resolve_auth(cfg: Config) -> tuple[str | None, str | None]:
    """(cookies_file, cookies_from_browser) from cfg (CLI/form) falling back to
    the persisted UI setting in the provider store."""
    cookies_file = cfg.get("ingest.cookies")
    browser = cfg.get("ingest.cookies_from_browser")
    if not cookies_file or not browser:
        try:
            from ..providers import store as store_mod
            ya = store_mod.load_store().get("youtube_auth", {}) or {}
            cookies_file = cookies_file or (ya.get("cookies_file") or None)
            browser = browser or (ya.get("cookies_from_browser") or None)
        except Exception:  # noqa: BLE001 - store optional; never block ingest on it
            pass
    if browser and str(browser).lower() not in _BROWSERS:
        browser = None
    return _materialise_cookies(cookies_file), (browser or None)


def _resolve_concurrent_fragments(cfg: Config) -> int:
    """How many DASH/HLS fragments yt-dlp fetches in parallel per download.

    yt-dlp's own default is 1 — fully sequential — which is the biggest single
    lever for "the download itself is slow" once auth/format selection are
    working: any format above ~360p is fragmented, and fetching those one at a
    time leaves a fast connection mostly idle. Same fallback pattern as
    ``_resolve_auth``: cfg (CLI/form) wins, else the persisted Settings UI
    value, else a modest built-in default — never unbounded, since driving
    this too high just looks like abuse to YouTube's edge servers.
    """
    val = cfg.get("ingest.concurrent_fragments")
    if not val:
        try:
            from ..providers import store as store_mod
            val = (store_mod.load_store().get("youtube_auth", {}) or {}).get("concurrent_fragments")
        except Exception:  # noqa: BLE001 - store optional; never block ingest on it
            pass
    try:
        n = int(val) if val else 4
    except (TypeError, ValueError):
        n = 4
    return max(1, min(n, 16))


def _auth_strategies(cfg: Config) -> list[tuple[str, dict]]:
    """Ordered (label, ydl-opts-overlay): configured cookies → browser → none."""
    cookies_file, browser = _resolve_auth(cfg)
    chain: list[tuple[str, dict]] = []
    if cookies_file:
        chain.append((f"cookies.txt ({os.path.basename(cookies_file)})",
                      {"cookiefile": cookies_file}))
    if browser:
        chain.append((f"{browser} browser cookies", {"cookiesfrombrowser": (browser,)}))
    chain.append(("no cookies", {}))
    return chain


# The "web" client YouTube gives yt-dlp by default is the one most tightly
# gated by PO-token / SABR restrictions — signed in fine, but only a handful
# of low-res formats (often just 360p) come back. Also querying "tv" (a
# device-code client that isn't PO-token-gated the same way) merges in
# whatever higher-res formats "web" withheld, in the SAME request — no extra
# round trip, no auth-chain change. Configurable since YouTube's per-client
# rules shift; comma-separated, e.g. "default,tv,web_safari".
def _extractor_args(cfg: Config) -> dict:
    raw = cfg.get("ingest.player_client") or "default,tv"
    clients = [c.strip() for c in str(raw).split(",") if c.strip()]
    return {"youtube": {"player_client": clients}} if clients else {}


def source_ceiling(cfg: Config) -> int:
    """Tallest source worth downloading, in pixels.

    The old hard-coded ``height<=1080`` was the single biggest cause of soft,
    pixelated output: a 1920x1080 download cropped to 9:16 leaves a 608 px-wide
    strip, which then has to be blown up 1.78x to reach 1080x1920. The detail
    was never on disk to begin with.

    So ask for what the chosen output actually needs (see
    ``reframe.resolution.source_height_needed``) rather than a fixed cap —
    a 720p export does not need a 4K download — bounded by
    ``ingest.max_height`` so an 8K source can't be pulled by accident.
    """
    from ..reframe.resolution import source_height_needed
    cap = int(cfg.get("ingest.max_height", 2160) or 2160)
    try:
        need = source_height_needed(cfg.get("reframe.resolution", "1080p"),
                                    cfg.get("reframe.aspect", "16:9"))
    except Exception:      # noqa: BLE001 - a bad aspect must not block ingest
        need = 1920
    # Round up to the next standard tier: asking for exactly 1920 would reject
    # the 2160p upload that is the only thing tall enough on offer.
    for tier in (1080, 1440, 2160, 4320):
        if tier >= need:
            return min(tier, cap)
    return cap


# Progressively looser selectors. YouTube does not always offer a video+audio
# pair at the height we want (SABR / per-client format restrictions), so
# falling back to "whatever plays" beats failing the download.
def _format_chain(cfg: Config) -> list[str]:
    h = source_ceiling(cfg)
    configured = (cfg.get("ingest.format")
                  or f"bv*[height<={h}]+ba/b[height<={h}]/b")
    chain = [configured, "bv*+ba/b", "best[ext=mp4]/best", "best"]
    out: list[str] = []
    for f in chain:                      # de-dup, keep order
        if f and f not in out:
            out.append(f)
    return out


def _with_format_fallback(cfg: Config, attempt):
    """Run ``attempt(format_selector)`` down `_format_chain`, loosening the
    selector each time YouTube refuses it.

    The download and the Settings "Test" button must agree about what works, so
    they both go through here — a Test that doesn't retry reports ✗ for a link
    the download would have fetched fine.

    Returns ``(result, format_used, fell_back)``. Re-raises the final `_NoFormat`
    if every selector is refused; any other exception propagates immediately.
    """
    formats = _format_chain(cfg)
    for i, fmt in enumerate(formats):
        try:
            return attempt(fmt), fmt, bool(i)
        except _NoFormat:
            if i + 1 >= len(formats):
                raise
            # DEBUG, not WARNING: the whole chain is now re-walked for every
            # player client, so at warning level this buried the one line that
            # matters (which client finally worked) under a dozen that don't.
            log.debug("format '%s' not offered for this video; trying a looser one", fmt)
    raise _NoFormat("no format selectors configured")   # pragma: no cover - chain is never empty


# Player clients to fall back through. Ordered so the ones that do NOT share
# the default web/tv infrastructure come first, since that is the whole point
# of switching. Overridable via `ingest.client_fallbacks` — YouTube changes
# which clients it will serve often enough that the operator should not need a
# code change to react.
_CLIENT_FALLBACKS = ("tv_simply", "mweb", "android_vr", "android", "ios", "web_safari")


def _known_clients() -> set[str]:
    """Player clients the *installed* yt-dlp understands.

    Sending one it doesn't know is not a soft failure — it aborts the download —
    so an unrecognised name is dropped rather than tried. Returns an empty set
    if the table can't be located, which means "don't filter": a moved import
    path must not disable the fallback chain entirely.
    """
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
        return set(INNERTUBE_CLIENTS)
    except Exception:      # noqa: BLE001 - yt-dlp absent, or its table moved
        return set()


def _client_fallbacks(cfg: Config) -> list[str]:
    raw = cfg.get("ingest.client_fallbacks")
    names = ([c.strip() for c in str(raw).split(",") if c.strip()] if raw
             else list(_CLIENT_FALLBACKS))
    known = _known_clients()
    return [c for c in names if not known or c in known]


def _with_client_fallback(cfg: Config, attempt):
    """Run ``attempt(extractor_args)``, rotating player clients when the current
    one cannot serve the video.

    Two symptoms, one cause. YouTube either strips the formats out of the
    response (`_NoFormat` — "signed in fine, but no usable format") or hands out
    a media URL and then refuses the fetch (`_Forbidden` — a 403 *after*
    extraction succeeded). Both mean: this player client is not one YouTube is
    willing to serve this video to. Neither the cookie chain nor the format
    chain can do anything about that — and both were being burned through
    pointlessly, 90 seconds at a time, before the job failed.

    Rotating the client is the only thing that addresses the actual cause, so
    it is the OUTER loop: the client decides which formats exist, which makes
    asking a dead client for four different selectors four wasted round-trips.
    """
    tried = _extractor_args(cfg)
    configured = ",".join(tried.get("youtube", {}).get("player_client", [])) or "default"
    try:
        return attempt(tried)
    except (_Forbidden, _NoFormat) as first:
        why = ("HTTP 403 on the media URL" if isinstance(first, _Forbidden)
               else "no usable format offered")
        already = set(tried.get("youtube", {}).get("player_client", []))
        for client in _client_fallbacks(cfg):
            if client in already:
                continue
            log.warning("player client '%s': %s — retrying with '%s'",
                        configured, why, client)
            try:
                result = attempt({"youtube": {"player_client": [client]}})
                log.info("download: the '%s' player client worked where '%s' could not",
                         client, configured)
                return result
            except (_Forbidden, _NoFormat) as e:
                why = ("HTTP 403 on the media URL" if isinstance(e, _Forbidden)
                       else "no usable format offered")
                configured = client
                continue
        raise first


def _base_opts(cfg: Config, dl_dir: str) -> dict:
    return {
        "format": cfg.get("ingest.format"),
        "outtmpl": os.path.join(dl_dir, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extractor_args": _extractor_args(cfg),
        # resumable partial downloads + yt-dlp's own fragment retries.
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 10,
        # configurable read timeout (20s default was too short on slow links).
        "socket_timeout": int(cfg.get("ingest.socket_timeout", 120) or 120),
        # yt-dlp default is 1 (serial) — see _resolve_concurrent_fragments.
        "concurrent_fragment_downloads": _resolve_concurrent_fragments(cfg),
    }


def ingest(source: str, cfg: Config, *, owner_confirmed: bool) -> SourceMeta:
    """Resolve ``source`` to a local media file + metadata.

    Args:
        source: URL to the operator's own video, or a path to a local file.
        cfg: pipeline configuration.
        owner_confirmed: operator's assertion that this is their content.

    Raises:
        ShortForgeError: if ownership is not confirmed, or the source is invalid.
    """
    if not owner_confirmed:
        raise ShortForgeError(
            "Ownership not confirmed. ShortForge only processes content you own "
            "or have licensed. Re-run with --owner-confirmed to proceed."
        )

    if _looks_like_url(source):
        meta = _ingest_url(source, cfg)
    else:
        meta = _ingest_local(source)

    meta.owner_confirmed = True
    log.info(
        "ingested: %s  (%.0fs, %s)",
        meta.title or os.path.basename(meta.file_path),
        meta.duration,
        meta.src_lang or "lang?",
    )
    return meta


def _ingest_local(path: str) -> SourceMeta:
    if not os.path.isfile(path):
        raise ShortForgeError(f"Source file not found: {path}")
    info = ffprobe_info(path)
    if not info.has_audio:
        # Not an error any more. A drone reel or a silent screen capture has no
        # audio track at all; clips are cut from the timeline instead of the
        # transcript (see select/nospeech.py). Announced, never silent.
        log.warning("%s has no audio track — no transcript, so clips will be cut "
                    "from the timeline at the requested length.",
                    os.path.basename(path))
    return SourceMeta(
        source=path,
        file_path=os.path.abspath(path),
        hash=content_key(path),
        title=os.path.splitext(os.path.basename(path))[0],
        duration=info.duration,
    )


def _require_ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except ImportError as e:  # pragma: no cover - dependency guard
        raise ShortForgeError(
            "yt-dlp is not installed. `pip install -U yt-dlp` to ingest from a URL."
        ) from e


def _download_with_retries(url: str, opts: dict, cfg: Config):
    """One auth strategy: download ``url`` with ``opts``, retrying *transient*
    failures with exponential backoff. Raises a classified exception."""
    yt_dlp = _require_ytdlp()
    attempts = max(1, int(cfg.get("ingest.retries", 4)))
    for attempt in range(1, attempts + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = ydl.prepare_filename(info)
            return info, file_path
        except Exception as e:  # yt-dlp raises many subclasses
            classified = _classify(e)
            if isinstance(classified, _Transient) and attempt < attempts:
                wait = 2 ** attempt
                log.warning("download attempt %d/%d failed (%s); retrying in %ds",
                            attempt, attempts, _clean_err(e), wait)
                time.sleep(wait)
                continue
            raise classified from e


def _ingest_url(url: str, cfg: Config) -> SourceMeta:
    _require_ytdlp()
    work_dir = cfg.get("paths.work_dir", ".shortforge")
    dl_dir = os.path.join(work_dir, "downloads")
    os.makedirs(dl_dir, exist_ok=True)
    base = _base_opts(cfg, dl_dir)

    # STEP 5(#5): fallback chain — configured cookies → browser cookies → none.
    # A bot wall on one strategy moves to the next; the winning strategy is logged.
    strategies = _auth_strategies(cfg)
    info = file_path = None
    saw_bot = False
    refused_every_client = False
    last_err: Exception | None = None
    for label, overlay in strategies:
        t0 = time.time()
        try:
            log.info("download: trying auth strategy '%s'", label)
            # Client outermost, format innermost: the player client determines
            # which formats exist at all, so each client gets the full chain.
            (info, file_path), fmt, fell_back = _with_client_fallback(
                cfg,
                lambda ex: _with_format_fallback(
                    cfg, lambda f: _download_with_retries(
                        url, {**base, **overlay, "format": f, "extractor_args": ex}, cfg)))
            if fell_back:
                log.info("download: format '%s' worked (the preferred one wasn't "
                         "offered for this video)", fmt)
            log.info("download succeeded via '%s' in %.1fs", label, time.time() - t0)
            break
        except _BotWall as e:
            saw_bot = True
            last_err = e
            log.warning("auth strategy '%s' hit YouTube's bot wall after %.1fs; trying next",
                        label, time.time() - t0)
            continue
        except _DRMProtected as e:
            # Fail immediately and say so plainly. Walking the rest of the auth
            # chain cannot help, and a vague failure here is what sent the
            # operator chasing a program bug that did not exist.
            raise ShortForgeError(
                f"This video is DRM-protected, so its stream cannot be downloaded "
                f"({_clean_err(e)[:160]}). This is a restriction on the video "
                f"itself — not a problem with ShortForge, your cookies or your "
                f"connection. Nothing can fix it; use a different source."
            ) from e
        except _Forbidden as e:
            last_err = e
            refused_every_client = True
            log.warning("auth strategy '%s': every player client was refused with a "
                        "403 (%.1fs)", label, time.time() - t0)
            continue
        except _Unavailable as e:
            raise ShortForgeError(
                f"'{url}' is unavailable or not a valid video URL ({e}). "
                f"Check the link is public/unlisted and that you own it."
            ) from e
        except _NoFormat as e:
            last_err = e
            refused_every_client = True
            log.warning("auth strategy '%s': no player client was offered a usable "
                        "video format for this link (%.1fs)", label, time.time() - t0)
            continue
        except _Transient as e:
            last_err = e
            log.warning("auth strategy '%s' failed on a network error after %.1fs; trying next",
                        label, time.time() - t0)
            continue
        except Exception as e:  # noqa: BLE001 - unknown extractor error; try next strategy
            last_err = e
            log.warning("auth strategy '%s' unavailable after %.1fs (%s); trying next",
                        label, time.time() - t0, _clean_err(e))
            continue

    if info is None or file_path is None:
        if saw_bot:
            raise ShortForgeError(BOT_AUTH_MESSAGE) from last_err
        if refused_every_client:
            # Naming the real cause matters: the old wording ("re-run to resume
            # the partial download; if it's a network problem this often clears
            # on retry") described neither, and sent the operator to retry a
            # link that could not work no matter how many times they tried.
            configured = _extractor_args(cfg).get("youtube", {}).get(
                "player_client", ["default"])
            tried = ", ".join(list(configured)
                              + [c for c in _client_fallbacks(cfg)
                                 if c not in set(configured)])
            raise ShortForgeError(
                f"YouTube would not serve {url} to any player client "
                f"({tried}) — it either offered no usable video format or "
                f"returned 403 for the one it gave. This is YouTube changing "
                f"how it hands out streams, not a problem with the link or "
                f"your cookies, and retrying will not clear it. The fix is "
                f"almost always a newer yt-dlp: Settings → 📺 YouTube "
                f"authentication → Update yt-dlp, or `pip install -U yt-dlp`. "
                f"Last error: {_clean_err(last_err) if last_err else 'unknown'}"
            ) from last_err
        raise ShortForgeError(
            f"Download failed for {url} after trying {len(strategies)} auth "
            f"strategy(ies): {last_err}. Re-run to resume the partial download; "
            f"if it's a network problem this often clears on retry."
        ) from last_err

    # merge_output_format may have rewritten the extension to .mp4.
    if not os.path.isfile(file_path):
        alt = os.path.splitext(file_path)[0] + ".mp4"
        if os.path.isfile(alt):
            file_path = alt
        else:
            raise ShortForgeError(f"Downloaded file not found (expected {file_path}).")

    probe = ffprobe_info(file_path)
    return SourceMeta(
        source=url,
        file_path=os.path.abspath(file_path),
        hash=content_key(file_path),
        title=info.get("title", ""),
        description=(info.get("description") or "")[:2000],
        duration=float(info.get("duration") or probe.duration),
        src_lang=info.get("language") or "",
        uploader=info.get("uploader", ""),
        video_id=info.get("id", ""),
    )


# --- Settings helpers (Test authentication / Update yt-dlp) ------------------ #

def test_youtube_auth(url: str, cfg: Config) -> tuple[bool, str]:
    """Metadata-only fetch (no download) with the configured auth. Tries EVERY
    strategy and reports each, so the operator can see whether their browser
    cookies actually work (not just that the no-cookies fallback happened to).
    Returns (any_ok, per-strategy detail) for the Settings 'Test' button."""
    try:
        yt_dlp = _require_ytdlp()
    except ShortForgeError as e:
        return False, str(e)
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extractor_args": _extractor_args(cfg),
            "socket_timeout": int(cfg.get("ingest.socket_timeout", 120) or 120)}
    lines: list[str] = []
    any_ok = False
    for label, overlay in _auth_strategies(cfg):

        def probe(fmt, overlay=overlay):
            try:
                with yt_dlp.YoutubeDL({**opts, **overlay, "format": fmt}) as ydl:
                    return ydl.extract_info(url, download=False)
            except Exception as e:  # noqa: BLE001 - classify so the chain can react
                raise _classify(e) from e

        try:
            # Walk the same format chain the download walks, so a ✓ here means
            # the download will succeed and a ✗ means it genuinely won't.
            info, fmt, fell_back = _with_format_fallback(cfg, probe)
            title = (info or {}).get("title", "?")
            dur = float((info or {}).get("duration") or 0)
            note = f"  [via looser format “{fmt}”]" if fell_back else ""
            lines.append(f"✓ {label}: “{title}” ({dur:.0f}s){note}")
            any_ok = True
        except Exception as e:  # noqa: BLE001
            c = _classify(e)
            why = ("bot wall" if isinstance(c, _BotWall) else
                   "signed in OK, but YouTube offered no downloadable format for this "
                   "video (try Update yt-dlp, or List formats below)"
                   if isinstance(c, _NoFormat) else
                   "unavailable" if isinstance(c, _Unavailable) else
                   "network" if isinstance(c, _Transient) else "error")
            lines.append(f"✗ {label}: {why} — {_clean_err(e)[:140]}")
    return any_ok, "\n".join(lines)


def list_formats(url: str, cfg: Config) -> tuple[bool, str]:
    """What streams YouTube is actually offering for ``url``, using the first auth
    strategy that can read it. Diagnostic for "Requested format is not available":
    an empty/audio-only list means YouTube withheld the video streams from this
    client, not that the selector is wrong."""
    try:
        yt_dlp = _require_ytdlp()
    except ShortForgeError as e:
        return False, str(e)
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extractor_args": _extractor_args(cfg),
            "socket_timeout": int(cfg.get("ingest.socket_timeout", 120) or 120)}
    last_err: Exception | None = None
    for label, overlay in _auth_strategies(cfg):
        try:
            # No "format" key at all: never selects, so it cannot raise _NoFormat.
            with yt_dlp.YoutubeDL({**opts, **overlay}) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        formats = (info or {}).get("formats") or []
        if not formats:
            return False, (f"{label}: read the page but YouTube listed 0 formats. "
                           f"Update yt-dlp — this is an extractor break, not a settings problem.")
        rows = [f"via {label} — {len(formats)} format(s):"]
        for f in formats[-40:]:           # last entries are the highest quality
            rows.append("  {:>7}  {:>9}  {:<5}  v={:<12} a={}".format(
                str(f.get("format_id", "?")),
                f"{f.get('width') or '-'}x{f.get('height') or '-'}",
                str(f.get("ext", "?")),
                str(f.get("vcodec", "?"))[:12],
                str(f.get("acodec", "?"))[:12]))
        has_video = any((f.get("vcodec") or "none") != "none" for f in formats)
        if not has_video:
            rows.append("\n⚠️ Audio-only: YouTube withheld every video stream from this "
                        "client. Update yt-dlp, then retry.")
        else:
            max_h = max((f.get("height") or 0) for f in formats)
            if 0 < max_h < 480:
                rows.append(f"\n⚠️ Best video offered is only {max_h}p even though the "
                            f"source is higher on YouTube. This client is being "
                            f"PO-token-gated, not out of formats — update yt-dlp, and "
                            f"check Settings → ingest player_client includes 'tv' "
                            f"(default: {cfg.get('ingest.player_client')}).")
        return True, "\n".join(rows)
    return False, f"Could not read formats with any auth strategy: {_clean_err(last_err) if last_err else '?'}"


def ytdlp_version() -> str | None:
    try:
        import yt_dlp
        return getattr(yt_dlp, "__version__", None)
    except ImportError:
        return None


def update_ytdlp() -> tuple[bool, str]:
    """Run ``pip install -U yt-dlp`` in the current interpreter; report the new
    version. Backing the Settings 'Update yt-dlp' button."""
    import subprocess
    import sys
    before = ytdlp_version() or "none"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            capture_output=True, text=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        return False, f"Update failed to launch: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        return False, "pip failed:\n" + "\n".join(tail)
    # Re-import to read the fresh version (a running process may still show the old
    # one until restart; report what pip installed from its output when possible).
    import importlib
    try:
        import yt_dlp
        importlib.reload(yt_dlp)
        after = getattr(yt_dlp, "__version__", "?")
    except Exception:  # noqa: BLE001
        after = "installed (restart to load)"
    return True, f"yt-dlp {before} → {after}. Restart the app to load it if unchanged."
