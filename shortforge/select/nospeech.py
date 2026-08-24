"""M4b — clip selection for sources with **no speech**.

The normal selector reads the transcript: it anchors on a spoken hook and grows
the clip to a whole thought. A music video, a sports highlight, a drone reel or
a silent screen-capture has no transcript, so that machinery has nothing to bite
on — the pipeline used to refuse the source outright.

Refusing is wrong. "Cut this into clips of N seconds" is a complete instruction
on its own, and the operator asked for exactly that. So this module selects
spans from the *timeline* instead of the transcript:

* **With an audio track** (music, crowd noise, engine noise) the loudest spans
  win. Loudness is a decent proxy for "something is happening" — the chorus, the
  goal, the drop — and it costs almost nothing: ONE audio-only ffmpeg pass, no
  video decoding at all.
* **With no audio track at all**, spans are spread evenly across the whole
  source so the clips sample the entire video rather than only its first minutes.

Deliberately *not* reused here: ``detect/visual.py``. It seeks per sample
(``CAP_PROP_POS_MSEC``), which measured 3.2 s per sample on the operator's CPU —
the same bottleneck already removed from the reframe path. Decoding video to
rank silent clips would cost more than rendering them.

Nothing about this is silent: the chosen basis is logged, returned, and recorded
in the manifest, so a clip list can never be mistaken for a speech-based one.
"""

from __future__ import annotations

import subprocess

from ..config import Config
from ..models import Clip
from ..utils import log, require_binary

# Loudness is sampled into slots this long. 1 s keeps a 30-min source to 1800
# floats — small enough to score every candidate window by brute force.
SLOT_SECONDS = 1.0

# Sample rate for the loudness pass. Speech intelligibility is irrelevant here;
# 8 kHz mono is enough to measure energy and keeps the pipe small.
_RATE = 8000

MIN_CLIP_SECONDS = 3.0


def has_speech(transcript) -> bool:
    """True if the ASR found anything worth cutting on.

    A transcript can come back non-empty but wordless (ASR noise artefacts), so
    check for actual text rather than just ``segments``.
    """
    if transcript is None or not getattr(transcript, "segments", None):
        return False
    return any((s.text or "").strip() for s in transcript.segments)


def audio_energy(path: str, duration: float, timeout: float = 900.0) -> list[float] | None:
    """Mean loudness per ``SLOT_SECONDS`` from one audio-only ffmpeg pass.

    Returns None when the source has no usable audio (or numpy is missing), in
    which case the caller falls back to even spacing.
    """
    try:
        import numpy as np
    except ImportError:            # pragma: no cover - numpy is a hard dependency
        log.debug("numpy unavailable; no-speech clips will be spaced evenly")
        return None
    try:
        ffmpeg = require_binary("ffmpeg")
    except Exception:  # noqa: BLE001
        return None

    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", path,
           "-vn", "-ac", "1", "-ar", str(_RATE), "-f", "s16le", "pipe:1"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("could not read audio levels (%s); spacing clips evenly instead", e)
        return None
    if proc.returncode != 0 or not proc.stdout:
        log.info("source has no usable audio track — spacing clips evenly across it")
        return None

    buf = np.frombuffer(proc.stdout, dtype="<i2")
    if buf.size == 0:
        return None
    per = int(_RATE * SLOT_SECONDS)
    usable = (buf.size // per) * per
    if usable < per:               # shorter than one slot
        return None
    frames = buf[:usable].reshape(-1, per).astype(np.float32) / 32768.0
    rms = np.sqrt((frames ** 2).mean(axis=1))
    if float(rms.max()) <= 1e-5:   # a silent audio track is the same as none
        log.info("audio track is silent throughout — spacing clips evenly across it")
        return None
    return [float(v) for v in rms]


def _window_score(energy: list[float], start: float, end: float) -> float:
    lo = int(start / SLOT_SECONDS)
    hi = max(lo + 1, int(end / SLOT_SECONDS))
    sel = energy[lo:hi]
    return sum(sel) / len(sel) if sel else 0.0


def plan_spans(duration: float, target: float, n: int,
               energy: list[float] | None = None) -> list[tuple[float, float, float]]:
    """Choose ``n`` non-overlapping spans of ``target`` seconds.

    Returns (start, end, score) in chronological order. ``score`` is 0..1 when
    ranked by loudness and 0.0 when evenly spaced.
    """
    if duration <= 0 or n <= 0:
        return []
    target = max(MIN_CLIP_SECONDS, min(float(target), duration))

    if energy is None:
        # No audio to rank on: spread across the WHOLE source, so 5 clips of a
        # 30-min video sample the whole thing rather than only its first 5 min.
        if n == 1:
            start = max(0.0, (duration - target) / 2.0)
            return [(start, min(duration, start + target), 0.0)]
        stride = (duration - target) / (n - 1)
        out = []
        for i in range(n):
            start = min(max(0.0, i * stride), max(0.0, duration - target))
            out.append((start, min(duration, start + target), 0.0))
        return out

    # Ranked by loudness: score every candidate window, take the best, then
    # forbid anything that would overlap it, and repeat.
    step = max(1.0, target / 4.0)
    cands: list[tuple[float, float]] = []      # (score, start)
    t = 0.0
    while t + target <= duration + 0.001:
        cands.append((_window_score(energy, t, t + target), t))
        t += step
    if not cands:
        cands = [(_window_score(energy, 0.0, duration), 0.0)]
    peak = max((s for s, _ in cands), default=0.0) or 1.0

    chosen: list[tuple[float, float, float]] = []
    taken: list[tuple[float, float]] = []
    for score, start in sorted(cands, key=lambda c: (-c[0], c[1])):
        if len(chosen) >= n:
            break
        end = min(duration, start + target)
        if any(start < b and end > a for a, b in taken):
            continue
        taken.append((start, end))
        chosen.append((start, end, round(min(1.0, score / peak), 3)))
    chosen.sort(key=lambda c: c[0])
    return chosen


def build_clips(meta, cfg: Config, source_hash: str) -> tuple[list[Clip], str]:
    """Clips for a source with no speech. Returns (clips, basis) where ``basis``
    names how they were picked, for the log, the summary line and the manifest."""
    duration = float(getattr(meta, "duration", 0) or 0)
    if duration <= 0:
        from ..utils import media_duration
        duration = media_duration(meta.file_path)

    target = float(cfg.get("select.target_duration", 45) or 45)
    requested = int(cfg.get("select.num_clips", 0) or 0)

    width = int(cfg.get("reframe.width", 1080))
    height = int(cfg.get("reframe.height", 1920))
    resolution = f"{width}x{height}"

    if duration < MIN_CLIP_SECONDS:
        raise _too_short(duration)

    # How many whole clips of this length the source can actually give.
    fits = max(1, int(duration // target)) if duration >= target else 1
    n = requested if requested > 0 else min(fits, 3)

    # Never silently hand back fewer than asked without saying why.
    if requested > 0 and requested > fits:
        log.warning(
            "asked for %d clip(s) of %.0fs but this %.0fs source only fits %d without "
            "overlapping — emitting %d. Ask for shorter clips, or a longer source.",
            requested, target, duration, fits, fits)
        n = fits

    if duration < target:
        log.warning(
            "source is %.0fs, shorter than the requested %.0fs clip — emitting one clip "
            "of the whole video rather than padding it.", duration, target)

    energy = audio_energy(meta.file_path, duration)
    spans = plan_spans(duration, target, n, energy)
    basis = "loudest moments" if energy else "evenly spaced"
    log.warning(
        "no speech in this source — selecting %d clip(s) of ~%.0fs by %s "
        "(no transcript, so no hook detection and no captions).",
        len(spans), target, basis)

    clips: list[Clip] = []
    for idx, (start, end, score) in enumerate(spans):
        clips.append(Clip(
            clip_id=f"{idx + 1:02d}",
            source_hash=source_hash,
            start=round(start, 3),
            end=round(end, 3),
            score=score,
            reason=f"no speech in source; picked by {basis}",
            caption_text="",
            resolution=resolution,
        ))
    return clips, basis


def _too_short(duration: float):
    from ..utils import ShortForgeError
    return ShortForgeError(
        f"This source is only {duration:.1f}s long and has no speech — there is "
        f"nothing to cut. Check the download completed (a {duration:.1f}s file is "
        f"usually a failed or partial download)."
    )


def unsupported_with_speech_only(what: str, extra: str = "") -> str:
    """Message for a request that genuinely cannot be met without speech."""
    return (f"{what} needs speech, and this source has none (the transcript is empty). "
            f"{extra}Re-run without it to get clips of the requested length from the "
            f"video as-is.").strip()


__all__ = ["has_speech", "audio_energy", "plan_spans", "build_clips",
           "unsupported_with_speech_only", "SLOT_SECONDS", "MIN_CLIP_SECONDS"]
