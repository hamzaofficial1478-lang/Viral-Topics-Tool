"""Disk housekeeping: see what the cache is holding, and reclaim it safely.

Every ingested source is kept in ``{work_dir}/downloads`` so a re-run (or a
resume after a power cut) doesn't re-download it. Nothing ever removed them.
On this operator's machine — modest disk, 1080p sources measured in gigabytes —
that only ends one way, and the first symptom was a *failed job*: the sole
cleanup in the codebase was `selfheal._clear_cache`, which fires reactively
after "no space left on device" has already killed a render.

So this module makes the cost visible before it bites, and reclaimable on
purpose rather than by accident:

* **Age, not guesswork.** Pruning only ever removes files older than a cutoff,
  so a download in flight (minutes old) can never be pulled out from under the
  running job. No job-to-file mapping to get wrong.
* **Cheap things go first.** ``downloads`` are gigabytes and free to re-fetch.
  ``hookscores`` are kilobytes and each one is a *paid* LLM call — they are
  never touched here, and `_clear_cache` no longer touches them either.
* **Never automatic.** Nothing in ShortForge deletes the operator's data on its
  own; this is called from an explicit CLI command or a Settings button, and
  supports a dry run so "what would go" is answerable without committing.
"""

from __future__ import annotations

import os
import time

from .utils import log

DOWNLOADS = "downloads"
# Old enough that an in-flight or just-finished download is never a candidate.
DEFAULT_KEEP_HOURS = 48


def _dir_size(path: str) -> tuple[int, int]:
    """(bytes, file_count) for a directory tree. Missing dir reads as empty."""
    total = files = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            p = os.path.join(root, name)
            try:
                total += os.path.getsize(p)
                files += 1
            except OSError:            # vanished mid-walk; not worth failing over
                continue
    return total, files


def _source_cache_dirs(work_dir: str) -> list[str]:
    """The per-source cache folders (named by content hash).

    These hold the genuinely large intermediates and were never pruned by
    anything: ``source_48k.wav`` alone is ~330 MB for a 29-minute video (48 kHz
    stereo PCM), plus ``audio16k.wav`` for Whisper, plus any TTS/dub/stem output.
    A handful of videos reaches several GB — which is exactly what the operator
    found. Identified structurally (a directory that isn't one of the known
    named caches) so a new intermediate can't quietly escape the sweep.
    """
    known = {DOWNLOADS, "hookscores"}
    out = []
    try:
        for name in os.listdir(work_dir):
            path = os.path.join(work_dir, name)
            if os.path.isdir(path) and name not in known:
                out.append(path)
    except OSError:
        pass
    return out


def prune_source_caches(work_dir: str = ".shortforge", *,
                        keep_hours: float = DEFAULT_KEEP_HOURS,
                        dry_run: bool = False) -> tuple[int, int]:
    """Delete per-source intermediate folders untouched for ``keep_hours``.

    Everything in them is re-derivable from the source video; the cost of
    losing one is re-transcribing, not lost work. Age is judged by the newest
    file inside, so a folder the running job is actively writing is never a
    candidate.
    """
    import shutil
    cutoff = time.time() - keep_hours * 3600
    n = freed = 0
    for path in _source_cache_dirs(work_dir):
        size, _ = _dir_size(path)
        try:
            newest = max((os.path.getmtime(os.path.join(r, f))
                          for r, _d, fs in os.walk(path) for f in fs),
                         default=os.path.getmtime(path))
        except OSError:
            continue
        if newest >= cutoff:
            continue
        n += 1
        freed += size
        if dry_run:
            continue
        shutil.rmtree(path, ignore_errors=True)
    if n and not dry_run:
        log.info("pruned %d source cache folder(s), freed %s", n, human_gb(freed))
    return n, freed


def cache_report(work_dir: str = ".shortforge") -> dict:
    """What the working directory is holding, split by what it costs to lose.

    ``reclaimable`` is downloads only — the part that is safe to delete because
    re-fetching costs time, not money. Transcripts and hook scores are counted
    separately precisely so they don't look like free space.
    """
    dl_bytes, dl_files = _dir_size(os.path.join(work_dir, DOWNLOADS))
    hook_bytes, hook_files = _dir_size(os.path.join(work_dir, "hookscores"))
    total_bytes, _ = _dir_size(work_dir)
    src_bytes = src_dirs = 0
    for path in _source_cache_dirs(work_dir):
        size, _ = _dir_size(path)
        src_bytes += size
        src_dirs += 1
    free = None
    try:
        import shutil
        free = shutil.disk_usage(work_dir if os.path.isdir(work_dir) else ".").free
    except OSError:                    # pragma: no cover - platform dependent
        pass
    return {
        "downloads_bytes": dl_bytes, "downloads_files": dl_files,
        "hookscores_bytes": hook_bytes, "hookscores_files": hook_files,
        "source_cache_bytes": src_bytes, "source_cache_dirs": src_dirs,
        "total_bytes": total_bytes,
        # Both are re-derivable from the source video; only the paid hook-score
        # cache is excluded. The per-source folders were the missing piece —
        # they hold the multi-hundred-MB WAV intermediates.
        "reclaimable_bytes": dl_bytes + src_bytes,
        "free_bytes": free,
    }


def human_gb(n: int | float | None) -> str:
    if n is None:
        return "?"
    if n < 1024 ** 2:
        return f"{n / 1024:.0f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{n / 1024 ** 3:.1f} GB"


def prune_downloads(work_dir: str = ".shortforge", *,
                    keep_hours: float = DEFAULT_KEEP_HOURS,
                    dry_run: bool = False) -> tuple[int, int]:
    """Delete cached source downloads older than ``keep_hours``.

    Returns ``(files, bytes)`` — what was removed, or what *would* be removed
    when ``dry_run``. Age is the only safety mechanism needed: a download the
    running job depends on is minutes old, far inside any sane cutoff.
    """
    root = os.path.join(work_dir, DOWNLOADS)
    if not os.path.isdir(root):
        return 0, 0
    cutoff = time.time() - keep_hours * 3600
    n = freed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path) or st.st_mtime >= cutoff:
            continue
        n += 1
        freed += st.st_size
        if dry_run:
            continue
        try:
            os.remove(path)
        except OSError as e:
            log.warning("could not remove cached download %s: %s", name, e)
            n -= 1
            freed -= st.st_size
    if n and not dry_run:
        log.info("pruned %d cached download(s), freed %s", n, human_gb(freed))
    return n, freed


def clear_downloads(work_dir: str = ".shortforge") -> int:
    """Remove every cached source download. Returns bytes freed.

    The emergency path (`selfheal` after "no space left on device"), where
    keeping anything is pointless. Still scoped to ``downloads`` — the paid
    hook-score cache is not collateral.
    """
    freed, _ = _dir_size(os.path.join(work_dir, DOWNLOADS))
    import shutil
    shutil.rmtree(os.path.join(work_dir, DOWNLOADS), ignore_errors=True)
    return freed
