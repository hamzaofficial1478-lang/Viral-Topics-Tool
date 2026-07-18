"""M2/M6 — Stem separation + re-mix (Phase 3, A1).

When the language changes we must remove the **original speech only** and keep
music / SFX / ambience. Demucs splits the source into a ``vocals`` stem and an
``accompaniment`` (no_vocals) stem; we discard vocals entirely and mix the new
dub over the accompaniment, ducking it under the dub with a sidechain compressor
so music breathes between sentences.

Hard rule (A1): if separation is unavailable we do **not** fall back to laying
the dub over the untouched original — that plays two voices at once. The caller
aborts unless the operator explicitly passes ``--allow-voice-bleed``.

Separation runs once per source and is cached (keyed on the source hash via the
Cache dir), so re-renders in other languages/styles never re-separate.
"""

from __future__ import annotations

import os
import shutil

from ..config import Config
from ..models import Clip
from ..utils import require_binary, run, log


def extract_clip_audio(source_path: str, clip: Clip, out_wav: str) -> str:
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(os.path.dirname(out_wav) or ".", exist_ok=True)
    run([
        ffmpeg, "-y", "-ss", f"{clip.start:.3f}", "-t", f"{clip.duration:.3f}",
        "-i", source_path, "-vn", "-ar", "48000", "-ac", "2", out_wav,
    ])
    return out_wav


def extract_source_audio(source_path: str, out_wav: str) -> str:
    """Full-source audio (48k stereo) — the input to one-time stem separation."""
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(os.path.dirname(out_wav) or ".", exist_ok=True)
    run([ffmpeg, "-y", "-i", source_path, "-vn", "-ar", "48000", "-ac", "2", out_wav])
    return out_wav


def demucs_available() -> bool:
    if shutil.which("demucs"):
        return True
    try:
        import demucs  # noqa: F401
        return True
    except ImportError:
        return False


def stem_separation_enabled(cfg: Config) -> tuple[bool, str]:
    """Resolve the stem_separation setting to (should_try, reason)."""
    setting = str(cfg.get("localize.stem_separation", "auto")).lower()
    if setting in ("false", "off", "no"):
        return False, "disabled (stem_separation=false)"
    have = demucs_available()
    if setting in ("true", "on", "yes"):
        return True, "forced (stem_separation=true)" if have else "requested but Demucs missing"
    return (have, "demucs present" if have else "demucs not installed")  # auto


def stem_model(cfg: Config) -> str:
    return str(cfg.get("localize.stem_model", cfg.get("localize.demucs_model", "htdemucs")))


def _eta_seconds(duration: float, cfg: Config) -> float:
    # Rough CPU estimate: htdemucs ~10x realtime on CPU; lighter models less.
    model = stem_model(cfg)
    factor = 4.0 if "mdx" in model else 10.0
    return duration * factor


def separate_source(source_path: str, source_wav: str, work_dir: str,
                    duration: float, cfg: Config) -> str | None:
    """Return an accompaniment (vocals-removed) WAV for the whole source.

    Cached/resumable: if Demucs already produced the file we reuse it. Returns
    None only if Demucs is genuinely unavailable or errors.
    """
    if not demucs_available():
        return None
    import sys

    model = stem_model(cfg)
    out_root = os.path.join(work_dir, "demucs")
    stem = os.path.splitext(os.path.basename(source_wav))[0]
    no_vocals = os.path.join(out_root, model, stem, "no_vocals.wav")
    if os.path.isfile(no_vocals) and os.path.getsize(no_vocals) > 0:
        log.info("stems: reusing cached accompaniment (%s)", model)
        return no_vocals

    os.makedirs(out_root, exist_ok=True)
    if not os.path.isfile(source_wav):
        extract_source_audio(source_path, source_wav)
    eta = _eta_seconds(duration, cfg)
    log.info("stems: separating vocals from music/SFX with Demucs (%s). "
             "CPU estimate ~%.0f min for %.0fs of audio; cached after this run.",
             model, eta / 60.0, duration)
    try:
        run([
            sys.executable, "-m", "demucs", "--two-stems=vocals",
            "-n", model, "-o", out_root, source_wav,
        ])
    except Exception as e:  # noqa: BLE001
        log.warning("demucs failed (%s)", e)
        return None
    return no_vocals if os.path.isfile(no_vocals) else None


def slice_audio(src_wav: str, start: float, duration: float, out_wav: str) -> str:
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(os.path.dirname(out_wav) or ".", exist_ok=True)
    run([
        ffmpeg, "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", src_wav, "-ar", "48000", "-ac", "2", out_wav,
    ])
    return out_wav


def _db_to_amp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def _duck_ratio(duck_db: float) -> float:
    """Map a target duck depth (dB) to a compressor ratio (heuristic)."""
    return 1.0 + abs(duck_db) * 1.4  # -6 dB -> ~9.4:1


def ducked_filtergraph(cfg: Config) -> str:
    """ffmpeg filter_complex that mixes voice (input 0) over bed (input 1),
    sidechain-ducking the bed under the voice. Pure/testable."""
    bed_gain = _db_to_amp(float(cfg.get("localize.accompaniment_gain_db", 0.0)))
    duck_db = float(cfg.get("localize.duck_db", -6.0))
    ratio = max(1.5, min(20.0, _duck_ratio(duck_db)))
    attack = float(cfg.get("localize.duck_attack_ms", 5))
    release = float(cfg.get("localize.duck_release_ms", 250))
    return (
        f"[1:a]volume={bed_gain:.4f}[bed];"
        f"[0:a]asplit=2[voice][key];"
        f"[bed][key]sidechaincompress=threshold=0.03:ratio={ratio:.2f}:"
        f"attack={attack:.0f}:release={release:.0f}[ducked];"
        f"[ducked][voice]amix=inputs=2:normalize=0:duration=first[out]"
    )


def remix_ducked(voice_wav: str, bed_wav: str, work_dir: str, cfg: Config) -> str:
    """Mix dub voice over the accompaniment bed, ducking the bed under speech.

    A sidechain compressor keyed by the dub voice dips the music/SFX bed while
    the dub speaks and restores it in the gaps.
    """
    ffmpeg = require_binary("ffmpeg")
    out = os.path.join(work_dir, "dub_mix.wav")
    run([
        ffmpeg, "-y", "-i", voice_wav, "-i", bed_wav,
        "-filter_complex", ducked_filtergraph(cfg), "-map", "[out]",
        "-ar", "48000", "-ac", "2", out,
    ])
    log.info("re-mixed dub over separated accompaniment (ducked %.0f dB)",
             float(cfg.get("localize.duck_db", -6.0)))
    return out


def remix_bleed(voice_wav: str, clip_audio_wav: str, work_dir: str, cfg: Config) -> str:
    """Fallback ONLY under --allow-voice-bleed: duck the untouched original.

    Leaves some original-voice bleed (two voices), hence gated. Kept so an
    operator who accepts the trade-off can still keep music/SFX without Demucs.
    """
    ffmpeg = require_binary("ffmpeg")
    bed_gain = _db_to_amp(float(cfg.get("localize.bleed_duck_db", -12.0)))
    out = os.path.join(work_dir, "dub_mix.wav")
    fc = (
        f"[1:a]volume={bed_gain:.4f}[bed];"
        f"[0:a][bed]amix=inputs=2:normalize=0:duration=first[out]"
    )
    run([
        ffmpeg, "-y", "-i", voice_wav, "-i", clip_audio_wav,
        "-filter_complex", fc, "-map", "[out]", "-ar", "48000", "-ac", "2", out,
    ])
    log.warning("re-mixed dub over DUCKED ORIGINAL (voice bleed allowed)")
    return out
