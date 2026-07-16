"""M2/M6 — Stem separation + re-mix (Phase 3).

Keeps music and sound effects when the language changes. Preferred path is
Demucs (splits vocals from everything else, so the music+SFX bed is clean).
When Demucs isn't installed, a no-dependency fallback ducks the original audio
under the new voiceover — music and SFX survive, with some original-voice bleed.
"""

from __future__ import annotations

import os
import shutil

from ..config import Config
from ..models import Clip
from ..utils import ffprobe_info, require_binary, run, log


def extract_clip_audio(source_path: str, clip: Clip, out_wav: str) -> str:
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(os.path.dirname(out_wav) or ".", exist_ok=True)
    run([
        ffmpeg, "-y", "-ss", f"{clip.start:.3f}", "-t", f"{clip.duration:.3f}",
        "-i", source_path, "-vn", "-ar", "48000", "-ac", "2", out_wav,
    ])
    return out_wav


def demucs_available() -> bool:
    if shutil.which("demucs"):
        return True
    try:
        import demucs  # noqa: F401
        return True
    except ImportError:
        return False


def separate_music(audio_wav: str, work_dir: str, cfg: Config) -> str | None:
    """Return a music+SFX bed (vocals removed) via Demucs, or None if unavailable."""
    if not demucs_available():
        return None
    import sys

    model = cfg.get("localize.demucs_model", "htdemucs")
    out_root = os.path.join(work_dir, "demucs")
    os.makedirs(out_root, exist_ok=True)
    try:
        run([
            sys.executable, "-m", "demucs", "--two-stems=vocals",
            "-n", model, "-o", out_root, audio_wav,
        ])
    except Exception as e:  # noqa: BLE001
        log.warning("demucs failed (%s); using duck fallback", e)
        return None
    stem = os.path.splitext(os.path.basename(audio_wav))[0]
    no_vocals = os.path.join(out_root, model, stem, "no_vocals.wav")
    return no_vocals if os.path.isfile(no_vocals) else None


def _db_to_amp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def remix(voice_wav: str, clip_audio_wav: str, work_dir: str, cfg: Config) -> tuple[str, str]:
    """Mix the dubbed voice over the preserved music/SFX bed.

    Returns (mixed_wav, method) where method is "demucs" or "duck".
    """
    ffmpeg = require_binary("ffmpeg")
    music = separate_music(clip_audio_wav, work_dir, cfg)
    if music:
        bed, method = music, "demucs"
        bed_gain = float(cfg.get("localize.music_gain", 1.0))
    else:
        bed, method = clip_audio_wav, "duck"
        bed_gain = _db_to_amp(float(cfg.get("localize.duck_db", -12.0)))

    out = os.path.join(work_dir, "dub_mix.wav")
    fc = (
        f"[1:a]volume={bed_gain:.4f}[bed];"
        f"[0:a][bed]amix=inputs=2:normalize=0:duration=first[out]"
    )
    run([
        ffmpeg, "-y", "-i", voice_wav, "-i", bed,
        "-filter_complex", fc, "-map", "[out]", "-ar", "48000", "-ac", "2", out,
    ])
    log.info("re-mixed dub over %s bed (%s)", method, os.path.basename(bed))
    return out, method
