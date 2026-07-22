"""C1 — Preflight checks (`shortforge doctor`).

Reports the state of every external dependency ShortForge can use, with a
concrete fix for each problem, and prints realistic per-minute processing
estimates for the detected hardware. The critical subset also runs at pipeline
start so a run fails fast with the same actionable message instead of a raw
crash deep inside a library.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

from .config import Config
from .utils import ShortForgeError, log

OK, WARN, FAIL = "ok", "warn", "fail"
_SYM = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _ffmpeg() -> Check:
    have = shutil.which("ffmpeg") and shutil.which("ffprobe")
    if have:
        return Check("ffmpeg + ffprobe", OK, "found on PATH")
    return Check("ffmpeg + ffprobe", FAIL, "not found on PATH",
                 "Install FFmpeg and ensure it's on PATH (Windows: download from "
                 "ffmpeg.org and add the bin folder to PATH; macOS: brew install ffmpeg).")


def _python() -> Check:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 13):
        return Check("Python", WARN, f"{ver} — some AI wheels (mediapipe, older "
                     "ctranslate2/torch) may not have 3.13 builds yet",
                     "If a dependency won't install, use a Python 3.11/3.12 venv.")
    if (v.major, v.minor) < (3, 10):
        return Check("Python", WARN, f"{ver} — 3.10+ recommended", "Upgrade to Python 3.11+.")
    return Check("Python", OK, ver)


def _ctranslate2() -> Check:
    try:
        import ctranslate2  # noqa: F401
        return Check("ctranslate2 (faster-whisper)", OK, "imports cleanly")
    except ImportError:
        return Check("ctranslate2 (faster-whisper)", WARN, "not installed",
                     "pip install faster-whisper")
    except Exception as e:  # noqa: BLE001 — DLL load failures land here
        msg = str(e)
        if "dll" in msg.lower() or "module" in msg.lower():
            return Check("ctranslate2 (faster-whisper)", FAIL,
                         f"native load failed: {msg}",
                         "On Windows install the Microsoft Visual C++ Redistributable "
                         "(x64) from microsoft.com — ctranslate2 needs it.")
        return Check("ctranslate2 (faster-whisper)", FAIL, msg, "Reinstall faster-whisper.")


def _ytdlp() -> Check:
    try:
        import yt_dlp
    except ImportError:
        return Check("yt-dlp", WARN, "not installed (needed for URL ingest)",
                     "pip install -U yt-dlp")
    ver = getattr(yt_dlp, "__version__", "?")
    # yt-dlp versions are date-stamped (YYYY.MM.DD). Extractors break often, so
    # warn when the install is stale — updating is the single biggest fix.
    import datetime
    import re
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", str(ver))
    if m:
        try:
            released = datetime.date(int(m[1]), int(m[2]), int(m[3]))
            age = (datetime.date.today() - released).days
            if age > 30:
                return Check("yt-dlp", WARN, f"version {ver} is {age} days old",
                             "YouTube extractors break often — run `pip install -U yt-dlp` "
                             "(or the 'Update yt-dlp' button in Settings).")
            return Check("yt-dlp", OK, f"version {ver} ({age} days old)")
        except ValueError:
            pass
    return Check("yt-dlp", OK, f"version {ver} (run `pip install -U yt-dlp` periodically)")


def _translation() -> Check:
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY")
                    or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    try:
        import argostranslate.translate as tr
        pairs = sorted({l.code for l in tr.get_installed_languages()})
        detail = f"Argos installed (langs: {', '.join(pairs) or 'none yet'})"
        if have_key:
            detail = "LLM key set + " + detail
        return Check("translation backend", OK, detail)
    except ImportError:
        if have_key:
            return Check("translation backend", OK, "LLM key set (Argos not installed)")
        return Check("translation backend", WARN,
                     "none — needed only for non-source-language output",
                     "pip install argostranslate  (or set an LLM key). Language "
                     "packs auto-install on first use.")


def _tts() -> Check:
    parts = []
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        parts.append("espeak")
    for mod, label in (("edge_tts", "edge"), ("TTS", "xtts")):
        try:
            __import__(mod)
            parts.append(label)
        except ImportError:
            pass
    if parts:
        return Check("TTS backends", OK, ", ".join(parts) + " available")
    return Check("TTS backends", WARN, "none (needed only for --dub)",
                 "pip install edge-tts  (natural voices), or install espeak-ng.")


def _demucs() -> Check:
    try:
        import demucs  # noqa: F401
        return Check("Demucs (stem separation)", OK, "installed")
    except ImportError:
        return Check("Demucs (stem separation)", WARN,
                     "not installed (needed to dub without voice bleed)",
                     "pip install demucs  (also installs torch; large download).")


def _disk(cfg: Config) -> Check:
    work = cfg.get("paths.work_dir", ".shortforge")
    path = work if os.path.isdir(work) else "."
    try:
        free_gb = shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return Check("disk space", WARN, "could not determine free space")
    if free_gb < 20:
        return Check("disk space", WARN, f"{free_gb:.0f} GB free (< 20 GB)",
                     "Free up space; downloads, stems and renders need room.")
    return Check("disk space", OK, f"{free_gb:.0f} GB free")


def _hardware() -> Check:
    gpu = False
    try:
        import torch
        gpu = bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        gpu = False
    if gpu:
        return Check("hardware", OK, "CUDA GPU detected — dubbing/voice-clone/lip-sync "
                     "run at usable speed (~1-2x realtime).")
    return Check("hardware", OK, "CPU-only. Estimates per minute of source: "
                 "transcribe+detect+render ~2-4 min; Demucs dubbing adds ~8-12x "
                 "realtime; voice-clone/lip-sync are impractical without a GPU.")


def _encoders() -> Check:
    """STEP 1: report hardware H.264 encoders ffmpeg exposes (Quick Sync etc.)."""
    try:
        from .render import available_hw_encoders
        hw = available_hw_encoders()
    except Exception:  # noqa: BLE001
        hw = []
    if hw:
        return Check("video encoders", OK,
                     f"hardware available: {', '.join(hw)} (+ libx264). Set render.encoder "
                     "'qsv'/'auto' after checking quality with `encode-sample`.")
    return Check("video encoders", OK, "libx264 (software) only — no hardware H.264 "
                 "encoder found (h264_qsv / nvenc / amf).")


def _whisper_model(cfg: Config) -> Check:
    model = str(cfg.get("transcribe.model", "small"))
    if model in ("tiny", "base"):
        return Check("transcription model", WARN,
                     f"'{model}' — low accuracy; garbles hard/accented speech, which "
                     f"breaks translation and clip selection downstream",
                     "Use 'small' (good CPU balance) or 'medium' (slower, more accurate): "
                     "--whisper-model small. Force the source language with --source-lang.")
    return Check("transcription model", OK,
                 f"'{model}'" + (" (medium/large are slow on CPU)" if model.startswith(("medium", "large")) else ""))


def run_checks(cfg: Config) -> list[Check]:
    return [
        _ffmpeg(), _python(), _ctranslate2(), _whisper_model(cfg), _ytdlp(),
        _translation(), _tts(), _demucs(), _disk(cfg), _hardware(), _encoders(),
    ]


def format_report(checks: list[Check]) -> str:
    lines = ["ShortForge doctor — environment check", "=" * 56]
    for c in checks:
        lines.append(f" [{_SYM.get(c.status, '?')}] {c.name}: {c.detail}")
        if c.status != OK and c.fix:
            lines.append(f"       fix: {c.fix}")
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    lines.append("=" * 56)
    lines.append(f" {fails} blocking issue(s), {warns} warning(s).")
    return "\n".join(lines)


def preflight(cfg: Config) -> None:
    """Run the critical checks at pipeline start; raise on a blocking failure."""
    for c in (_ffmpeg(), _ctranslate2()):
        if c.status == FAIL:
            raise ShortForgeError(f"{c.name}: {c.detail}\n  fix: {c.fix}")
        if c.status == WARN:
            log.warning("%s: %s", c.name, c.detail)
