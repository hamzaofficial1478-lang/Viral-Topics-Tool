"""Shared helpers: logging, subprocess, ffmpeg/ffprobe, content hashing."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger("shortforge")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # Keep third-party libraries (faster-whisper, argostranslate, httpx) quiet;
    # only ShortForge's own logger emits at INFO/DEBUG.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("shortforge").setLevel(level)


def load_env_file(path: str = ".env") -> int:
    """Load ``KEY=VALUE`` lines from a .env file into os.environ.

    Dependency-free (no python-dotenv needed). Existing environment variables
    are never overwritten, so a real shell export always wins. Lines that are
    blank, comments (``#``), or ``export KEY=VALUE`` are handled; surrounding
    quotes are stripped. Returns the number of keys set. Silently does nothing
    if the file is absent — this is how the operator supplies API keys on their
    own machine without ever pasting them into a chat or committing them.
    """
    if not os.path.isfile(path):
        return 0
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
                    count += 1
    except OSError:
        return count
    if count:
        log.debug("loaded %d key(s) from %s", count, path)
    return count


class ShortForgeError(RuntimeError):
    """Raised for expected, user-facing failures (bad input, missing tool)."""


def pid_alive(pid: int) -> bool:
    """Best-effort liveness check, Windows- and POSIX-safe. On any doubt, say
    alive — stealing a lock/slot that's still legitimately held is the
    dangerous direction to be wrong in.

    Not ``os.kill(pid, 0)`` on Windows: unlike POSIX, Windows gives signal 0
    no special "just check" meaning in Python's os.kill, so it is NOT a safe
    liveness probe there — this uses ``OpenProcess`` instead, which only
    queries.
    """
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)          # POSIX: signal 0 checks, never actually signals
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def require_binary(name: str) -> str:
    """Return the path to a required system binary or raise a clear error."""
    path = shutil.which(name)
    if not path:
        raise ShortForgeError(
            f"Required tool '{name}' not found on PATH. "
            f"Install ffmpeg (which provides ffmpeg + ffprobe): "
            f"`sudo apt-get install -y ffmpeg` or `brew install ffmpeg`."
        )
    return path


# Wall-clock ceilings so a wedged child can never hang the whole queue.
#
# Before these, `run()` had no timeout at all: an ffmpeg that deadlocked (a
# malformed input, a stalled hardware encoder) blocked its thread forever. The
# job never finished, the queue never advanced, the dashboard sat on "Working…"
# and no notification ever fired — indistinguishable from "still rendering",
# which is precisely the state the operator has no way to diagnose from a phone.
# With a ceiling the job fails loudly instead, and the queue moves to the next
# link.
#
# The values are deliberately far above anything legitimate. Only ffmpeg,
# ffprobe and espeak go through here — Whisper and Demucs are in-process
# libraries and are NOT affected — and a single per-clip render measures ~100s
# on this operator's CPU, so the ffmpeg ceiling is ~70x observed. Override per
# environment if a genuinely longer call ever exists.
_TIMEOUTS = {
    "ffprobe": float(os.environ.get("SHORTFORGE_FFPROBE_TIMEOUT", 120)),
    "ffmpeg": float(os.environ.get("SHORTFORGE_FFMPEG_TIMEOUT", 7200)),
}
_DEFAULT_TIMEOUT = float(os.environ.get("SHORTFORGE_SUBPROCESS_TIMEOUT", 600))


def _timeout_for(cmd: list[str]) -> float:
    base = os.path.basename(cmd[0]) if cmd else ""
    for name, secs in _TIMEOUTS.items():
        if base.startswith(name):
            return secs
    return _DEFAULT_TIMEOUT


def run(cmd: list[str], *, quiet: bool = True,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a command, raising ShortForgeError with captured stderr on failure.

    STEP 0: every ffmpeg invocation is logged with its full command line and
    wall-clock duration and recorded on the active Timings, so the render/encode
    cost is measurable per call (ffprobe stays at DEBUG — cheap and noisy)."""
    from . import timing
    log.debug("run: %s", " ".join(cmd))
    _t0 = time.time()
    _limit = timeout if timeout is not None else _timeout_for(cmd)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_limit,
        )
    except subprocess.TimeoutExpired as e:
        # subprocess.run has already killed the child by the time this raises.
        raise ShortForgeError(
            f"{os.path.basename(cmd[0]) if cmd else 'command'} did not finish within "
            f"{_limit:.0f}s and was stopped: {' '.join(cmd[:3])} ...\n"
            f"This normally means it wedged rather than that it needed longer. "
            f"Raise SHORTFORGE_FFMPEG_TIMEOUT / SHORTFORGE_FFPROBE_TIMEOUT if the "
            f"work genuinely takes longer on this machine."
        ) from e
    _dt = time.time() - _t0
    _base = os.path.basename(cmd[0]) if cmd else ""
    if _base.startswith("ffmpeg"):
        log.info("ffmpeg %.1fs: %s", _dt, " ".join(cmd))
        timing.record_ffmpeg(cmd, _dt)
    elif _base.startswith("ffprobe"):
        log.debug("ffprobe %.2fs: %s", _dt, " ".join(cmd))
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-15:]
        raise ShortForgeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd[:3])} ...\n"
            + "\n".join(tail)
        )
    if not quiet and proc.stderr:
        log.debug(proc.stderr.strip())
    return proc


@dataclass
class MediaInfo:
    width: int
    height: int
    duration: float
    fps: float
    has_audio: bool


def ffprobe_info(path: str) -> MediaInfo:
    """Probe a media file for the fields the pipeline needs."""
    ffprobe = require_binary("ffprobe")
    proc = run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            path,
        ]
    )
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ShortForgeError(f"No video stream found in {path}")

    width = int(video.get("width", 0))
    height = int(video.get("height", 0))

    # Duration can live on the stream or the container.
    duration = 0.0
    for src in (video, data.get("format", {})):
        d = src.get("duration")
        if d:
            try:
                duration = float(d)
                break
            except (TypeError, ValueError):
                pass

    fps = _parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    return MediaInfo(
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        has_audio=audio is not None,
    )


def _parse_fraction(value: str | None) -> float:
    if not value or value in ("0/0", "N/A"):
        return 30.0
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 30.0
        return float(value)
    except (TypeError, ValueError):
        return 30.0


def media_duration(path: str) -> float:
    """Duration in seconds for any media (audio or video) via the container."""
    ffprobe = require_binary("ffprobe")
    proc = run([
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1", path,
    ])
    try:
        return float(proc.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


def content_key(path: str) -> str:
    """A fast, stable cache key for a media file.

    Hashes the file size plus sampled head/tail bytes rather than the whole
    file, so re-running on a multi-GB source stays instant.
    """
    size = os.path.getsize(path)
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    chunk = 8 * 1024 * 1024  # 8 MiB from each end
    with open(path, "rb") as f:
        h.update(f.read(chunk))
        if size > chunk:
            f.seek(max(0, size - chunk))
            h.update(f.read(chunk))
    return h.hexdigest()


def format_timestamp(seconds: float) -> str:
    """Seconds -> ffmpeg-friendly HH:MM:SS.mmm."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"
