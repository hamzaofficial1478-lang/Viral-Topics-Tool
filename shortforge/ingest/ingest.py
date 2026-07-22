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
  * a *friendly* message on YouTube's "confirm you're not a bot" wall that
    points at Settings → YouTube authentication (never the raw yt-dlp error);
  * a configurable read timeout (default 120s), resumable partial downloads,
    retries with exponential backoff, and a per-video-id download cache.
"""

from __future__ import annotations

import os

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


def _classify(e: Exception) -> Exception:
    msg = str(e).lower()
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
    return (cookies_file or None), (browser or None)


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


def _base_opts(cfg: Config, dl_dir: str) -> dict:
    return {
        "format": cfg.get("ingest.format"),
        "outtmpl": os.path.join(dl_dir, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # resumable partial downloads + yt-dlp's own fragment retries.
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 10,
        # configurable read timeout (20s default was too short on slow links).
        "socket_timeout": int(cfg.get("ingest.socket_timeout", 120) or 120),
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
        raise ShortForgeError(
            f"{path} has no audio stream — cannot transcribe or clip on speech."
        )
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
    import time

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
                            attempt, attempts, e, wait)
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
    last_err: Exception | None = None
    for label, overlay in strategies:
        try:
            log.info("download: trying auth strategy '%s'", label)
            info, file_path = _download_with_retries(url, {**base, **overlay}, cfg)
            log.info("download succeeded via '%s'", label)
            break
        except _BotWall as e:
            saw_bot = True
            last_err = e
            log.warning("auth strategy '%s' hit YouTube's bot wall; trying next", label)
            continue
        except _Unavailable as e:
            raise ShortForgeError(
                f"'{url}' is unavailable or not a valid video URL ({e}). "
                f"Check the link is public/unlisted and that you own it."
            ) from e
        except _Transient as e:
            last_err = e
            log.warning("auth strategy '%s' failed on a network error; trying next", label)
            continue
        except Exception as e:  # noqa: BLE001 - unknown extractor error; try next strategy
            last_err = e
            log.warning("auth strategy '%s' failed (%s); trying next", label, e)
            continue

    if info is None or file_path is None:
        if saw_bot:
            raise ShortForgeError(BOT_AUTH_MESSAGE) from last_err
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
            "socket_timeout": int(cfg.get("ingest.socket_timeout", 120) or 120)}
    lines: list[str] = []
    any_ok = False
    for label, overlay in _auth_strategies(cfg):
        try:
            with yt_dlp.YoutubeDL({**opts, **overlay}) as ydl:
                info = ydl.extract_info(url, download=False)
            title = (info or {}).get("title", "?")
            dur = float((info or {}).get("duration") or 0)
            lines.append(f"✓ {label}: “{title}” ({dur:.0f}s)")
            any_ok = True
        except Exception as e:  # noqa: BLE001
            c = _classify(e)
            why = ("bot wall" if isinstance(c, _BotWall) else
                   "unavailable" if isinstance(c, _Unavailable) else
                   "network" if isinstance(c, _Transient) else "error")
            lines.append(f"✗ {label}: {why} — {str(e).splitlines()[-1][:100]}")
    return any_ok, "\n".join(lines)


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
