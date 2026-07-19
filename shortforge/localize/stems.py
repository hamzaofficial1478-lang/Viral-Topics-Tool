"""M2/M6 — Stem separation + re-mix (Phase 3, A1 + G1).

When the language changes we must remove the **original speech only** and keep
music / SFX / ambience. Demucs (``--two-stems=vocals``) splits the source into a
``vocals`` stem and a ``no_vocals`` stem which is drums+bass+other combined — so
we already keep the full instrumental/SFX bed, not just ``other``.

G1 diagnosis: "some scenes silent" was NOT a summing bug. Demucs routes ambience
and effects that are correlated with speech (reverb tails, room tone, breaths)
into the *vocals* stem, which we discarded at -inf — so on those scenes the bed
vanished. Fix: ``vocal_removal_strength`` retains the vocals stem at a low level
(default partial, -18 dB) so its ambience survives; ``--debug-audio`` exports
each stem + the final mix so the operator can hear exactly what is kept.

Hard rule (A1): if separation is unavailable we do **not** fall back to laying
the dub over the untouched original — that plays two voices at once. The caller
aborts unless the operator explicitly passes ``--allow-voice-bleed``.

Separation runs once per source and is cached (keyed on the source hash via the
Cache dir), so re-renders in other languages/styles never re-separate.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from ..config import Config
from ..models import Clip
from ..utils import require_binary, run, log


@dataclass
class Stems:
    """Paths to the separated stems (from Demucs ``--two-stems=vocals``)."""
    no_vocals: str          # drums + bass + other (the instrumental/SFX bed)
    vocals: str             # isolated vocals (also carries some ambience/reverb)


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
                    duration: float, cfg: Config) -> Stems | None:
    """Return the separated Stems (no_vocals + vocals) for the whole source.

    Cached/resumable: if Demucs already produced the files we reuse them. Returns
    None only if Demucs is genuinely unavailable or errors.
    """
    if not demucs_available():
        return None
    import sys

    model = stem_model(cfg)
    out_root = os.path.join(work_dir, "demucs")
    stem = os.path.splitext(os.path.basename(source_wav))[0]
    no_vocals = os.path.join(out_root, model, stem, "no_vocals.wav")
    vocals = os.path.join(out_root, model, stem, "vocals.wav")
    if os.path.isfile(no_vocals) and os.path.getsize(no_vocals) > 0:
        log.info("stems: reusing cached separation (%s)", model)
        return Stems(no_vocals=no_vocals, vocals=vocals)

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
    if not os.path.isfile(no_vocals):
        return None
    return Stems(no_vocals=no_vocals, vocals=vocals)


def _retain_db(cfg: Config) -> float | None:
    """Level to retain the vocals stem at, or None for full removal (-inf)."""
    strength = str(cfg.get("localize.vocal_removal_strength", "partial")).lower()
    if strength in ("full", "off", "none", "-inf"):
        return None
    # "partial" (or an explicit dB number) -> keep the vocals stem low so the
    # ambience/reverb Demucs mis-routed into it survives (G1 fix).
    try:
        return float(strength)  # allow an explicit dB value
    except ValueError:
        return float(cfg.get("localize.vocal_retain_db", -18.0))


def build_bed(stems: Stems, work_dir: str, cfg: Config) -> str:
    """Reconstruct the music/SFX bed = no_vocals (+ optional low vocals ambience).

    Cached to ``bed.wav``. With full removal the bed is just no_vocals; partial
    (default) mixes the vocals stem back in at ``vocal_retain_db`` so scenes whose
    ambience Demucs assigned to vocals aren't dead-silent.
    """
    ffmpeg = require_binary("ffmpeg")
    out = os.path.join(work_dir, "bed.wav")
    retain = _retain_db(cfg)
    if retain is None or not os.path.isfile(stems.vocals):
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        shutil.copyfile(stems.no_vocals, out)
        log.info("bed: full vocal removal (accompaniment only)")
        return out
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    gain = _db_to_amp(retain)
    fc = (f"[1:a]volume={gain:.4f}[amb];"
          f"[0:a][amb]amix=inputs=2:normalize=0:duration=longest[bed]")
    run([
        ffmpeg, "-y", "-i", stems.no_vocals, "-i", stems.vocals,
        "-filter_complex", fc, "-map", "[bed]", "-ar", "48000", "-ac", "2", out,
    ])
    log.info("bed: accompaniment + vocals-stem ambience retained at %.0f dB "
             "(some original-voice bleed kept by design; set vocal_removal_strength=full to drop it)",
             retain)
    return out


def export_debug_audio(stems: Stems, bed: str, out_dir: str) -> list[str]:
    """--debug-audio: copy the stems + reconstructed bed for the operator to hear."""
    dbg = os.path.join(out_dir, "audio_debug")
    os.makedirs(dbg, exist_ok=True)
    written = []
    for label, src in (("vocals", stems.vocals), ("accompaniment", stems.no_vocals),
                       ("bed", bed)):
        if src and os.path.isfile(src):
            dst = os.path.join(dbg, f"{label}.wav")
            shutil.copyfile(src, dst)
            written.append(dst)
    log.info("--debug-audio: wrote %d stem/bed WAV(s) to %s", len(written), dbg)
    return written


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
