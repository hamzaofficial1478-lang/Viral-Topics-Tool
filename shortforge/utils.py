"""Shared helpers: logging, subprocess, ffmpeg/ffprobe, content hashing."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
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


class ShortForgeError(RuntimeError):
    """Raised for expected, user-facing failures (bad input, missing tool)."""


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


def run(cmd: list[str], *, quiet: bool = True) -> subprocess.CompletedProcess:
    """Run a command, raising ShortForgeError with captured stderr on failure."""
    log.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
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
