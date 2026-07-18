"""M1 — Ingestion.

Accepts a link to the operator's *own* video (or a local file they own),
downloads best-quality source with yt-dlp, records metadata, and computes a
content hash for caching.

Ownership is enforced here: this is the one hard rule of the whole tool. We do
not ingest arbitrary third-party links — the operator must pass an explicit
ownership confirmation.
"""

from __future__ import annotations

import os

from ..config import Config
from ..models import SourceMeta
from ..utils import ShortForgeError, content_key, ffprobe_info, log

_URL_PREFIXES = ("http://", "https://", "www.")


def _looks_like_url(source: str) -> bool:
    return source.startswith(_URL_PREFIXES)


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


def _ingest_url(url: str, cfg: Config) -> SourceMeta:
    try:
        import yt_dlp
    except ImportError as e:  # pragma: no cover - dependency guard
        raise ShortForgeError(
            "yt-dlp is not installed. `pip install yt-dlp` to ingest from a URL, "
            "or pass a local file path instead."
        ) from e

    work_dir = cfg.get("paths.work_dir", ".shortforge")
    dl_dir = os.path.join(work_dir, "downloads")
    os.makedirs(dl_dir, exist_ok=True)

    ydl_opts = {
        "format": cfg.get("ingest.format"),
        "outtmpl": os.path.join(dl_dir, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # C3: resumable partial downloads + yt-dlp's own fragment retries.
        "continuedl": True,
        "retries": 3,
        "fragment_retries": 5,
    }
    cookies = cfg.get("ingest.cookies")
    if cookies:
        ydl_opts["cookiefile"] = cookies  # M1 auth for private/unlisted videos

    # C3: retry transient network failures with exponential backoff, and tell a
    # network error apart from an invalid/unavailable URL.
    import time

    attempts = int(cfg.get("ingest.retries", 3))
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = ydl.prepare_filename(info)
            break
        except Exception as e:  # yt-dlp raises many subclasses
            last_err = e
            msg = str(e).lower()
            transient = any(s in msg for s in (
                "forcibly closed", "timed out", "timeout", "connection",
                "temporarily", "10054", "reset by peer", "network", "unreachable",
            ))
            unavailable = any(s in msg for s in (
                "not available", "private video", "removed", "does not exist",
                "unavailable", "404", "no video", "unsupported url",
            ))
            if unavailable or not transient or attempt == attempts:
                if unavailable:
                    raise ShortForgeError(
                        f"'{url}' is unavailable or not a valid video URL "
                        f"({e}). Check the link, or download it yourself and pass "
                        f"the local file path instead."
                    ) from e
                raise ShortForgeError(
                    f"Download failed for {url} after {attempt} attempt(s): {e}\n"
                    f"This looks like a network problem. Re-run to resume the "
                    f"partial download, or download the file manually and pass its "
                    f"local path."
                ) from e
            wait = 2 ** attempt
            log.warning("download attempt %d/%d failed (%s); retrying in %ds",
                        attempt, attempts, e, wait)
            time.sleep(wait)

    # merge_output_format may have rewritten the extension to .mp4.
    if not os.path.isfile(file_path):
        base = os.path.splitext(file_path)[0]
        alt = base + ".mp4"
        if os.path.isfile(alt):
            file_path = alt
        else:
            raise ShortForgeError(
                f"Downloaded file not found (expected {file_path})."
            )

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
