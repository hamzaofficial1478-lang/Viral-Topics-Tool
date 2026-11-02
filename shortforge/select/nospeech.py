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

import re
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


# Whisper does not return silence for music — it invents text. These are its
# well-documented stock hallucinations (YouTube-trained outros, subtitle
# credits) plus the sound-event tags some backends emit. A segment made only of
# these is not speech. Matched against the whole normalised segment text, so a
# real sentence that merely *contains* "thank you" is never discarded.
_HALLUCINATIONS = {
    "thank you", "thank you so much", "thank you very much", "thanks",
    "thank you for watching", "thanks for watching", "thank you for watching this video",
    "please subscribe", "subscribe", "like and subscribe", "please like and subscribe",
    "don't forget to subscribe", "see you next time", "see you in the next video",
    "bye", "bye bye", "you", "okay", "oh", "ah", "uh", "um", "hmm", "yeah",
    "music", "applause", "laughter", "silence", "no audio", "blank audio",
    "subtitles by the amara.org community", "amara.org",
    "sous-titres réalisés par la communauté d'amara.org",
    "sous-titrage st' 501", "sous-titrage société radio-canada",
    "untertitel im auftrag des zdf", "untertitel der amara.org-community",
    "merci", "merci d'avoir regardé", "gracias", "gracias por ver",
}
_TAG_RE = re.compile(r"[\[\(【（][^\]\)】）]*[\]\)】）]")      # [Music] (applause) 【音楽】
_NOISE_RE = re.compile(r"[♪♫♬♩🎵🎶*~…\-–—.,!?¡¿'\"“”‘’:;]+")


def _normalise(text: str) -> str:
    t = _TAG_RE.sub(" ", text or "")
    t = _NOISE_RE.sub(" ", t)
    return " ".join(t.lower().split())


# Normalised with the SAME function the segments go through, so an entry with
# punctuation ("amara.org") can actually match.
_HALLUCINATIONS_NORM = {_normalise(h) for h in _HALLUCINATIONS}


def _real_words(text: str) -> str:
    """Segment text with sound-event tags, music notes and punctuation removed,
    lower-cased. Empty (or a stock hallucination) means 'not speech'."""
    t = _normalise(text)
    return "" if t in _HALLUCINATIONS_NORM else t


def speech_seconds(transcript) -> tuple[float, int]:
    """(seconds covered by real speech, number of real-speech segments)."""
    total, n = 0.0, 0
    for s in getattr(transcript, "segments", None) or []:
        if _real_words(s.text):
            total += max(0.0, float(s.end) - float(s.start))
            n += 1
    return total, n


def has_speech(transcript) -> bool:
    """True if the ASR found any real words — hallucinations and sound tags
    ("[Music]", "Thank you for watching!", "♪") do not count."""
    return speech_seconds(transcript)[1] > 0


def is_voiceover(transcript, duration: float, min_coverage: float = 0.25) -> tuple[bool, str]:
    """Does this source have a voice-over worth cutting on? Returns (yes, why).

    "Any words at all" was the wrong test. Whisper transcribes a music video as
    a scatter of hallucinations and lyric fragments, so a video with no voice-
    over was routed to the speech selector — which can only grow clips out of
    contiguous speech and so delivered 3 of 10 requested clips (one of them 87s
    against a 120s request). What decides it is how much of the video is
    actually talk: under ``min_coverage`` the timeline selector is the one that
    can honour both the count and the length.
    """
    secs, n = speech_seconds(transcript)
    if n == 0:
        return False, "no words were recognised"
    dur = float(duration or getattr(transcript, "duration", 0) or 0)
    if dur <= 0:
        return True, f"{n} spoken segment(s)"
    cov = secs / dur
    if cov < min_coverage:
        return False, (f"speech covers only {cov:.0%} of the video ({n} short fragment(s) "
                       f"— music lyrics or ASR noise), under the {min_coverage:.0%} needed "
                       f"to count as a voice-over")
    return True, f"speech covers {cov:.0%} of the video"


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


def _spread(duration: float, target: float, n: int,
            energy: list[float] | None) -> list[tuple[float, float, float]]:
    """``n`` evenly spread spans covering the whole source.

    Each span gets its own slot; when there is audio the span is nudged to the
    liveliest moment *inside its slot*, which keeps the clips distinct and in
    order while still honouring the requested count. Slots overlap when more
    clips were asked for than fit end-to-end — that is the point.
    """
    if n == 1:
        start = max(0.0, (duration - target) / 2.0)
        return [(start, min(duration, start + target), 0.0)]

    last_start = max(0.0, duration - target)
    stride = last_start / (n - 1)
    picks: list[tuple[float, float]] = []       # (start, raw score)
    for i in range(n):
        base = min(i * stride, last_start)
        if energy is None or stride <= 0:
            picks.append((base, 0.0))
            continue
        lo = max(0.0, base - stride / 2.0)
        hi = min(last_start, base + stride / 2.0)
        step = max(1.0, stride / 8.0)
        best_start, best_score = base, -1.0
        t = lo
        while t <= hi + 1e-6:
            score = _window_score(energy, t, t + target)
            if score > best_score:
                best_start, best_score = t, score
            t += step
        picks.append((best_start, max(0.0, best_score)))

    peak = max((s for _, s in picks), default=0.0) or 1.0
    return [(start, min(duration, start + target), round(min(1.0, score / peak), 3))
            for start, score in picks]


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

    fits = int(duration // target) if duration >= target else 0

    if energy is None or n > fits:
        # Either nothing to rank on, or more clips were asked for than fit
        # end-to-end. Spread them across the WHOLE source: 5 clips of a 30-min
        # video should sample the whole thing, not just its first 5 minutes —
        # and when more are asked for than fit, they share footage rather than
        # going missing. The caller says so out loud.
        return _spread(duration, target, n, energy)

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

    # How many whole clips of this length fit end-to-end without sharing footage.
    fits = max(1, int(duration // target)) if duration >= target else 1
    n = requested if requested > 0 else min(fits, 3)

    # The count and the length are both instructions, so both are honoured. When
    # more clips are asked for than fit end-to-end the only way to deliver them
    # is to let them share footage — which is said plainly rather than being
    # resolved by quietly handing back fewer clips.
    if requested > fits and duration >= target:
        log.warning(
            "asked for %d clip(s) of %.0fs from a %.0fs source — that is more than the "
            "%d that fit end-to-end, so clips will overlap and share some footage. "
            "Ask for %d or fewer (or shorter clips) if you want them all distinct.",
            requested, target, duration, fits, fits)

    if duration < target:
        # Overlapping clips are still different from each other; copies of the
        # whole video are not. Emitting the requested count here would just hand
        # back N identical files, so this is the one case that returns fewer.
        n = 1
        log.warning(
            "source is %.0fs, shorter than the requested %.0fs clip — emitting one clip "
            "of the whole video rather than %d identical copies of it (or padding it).",
            duration, target, requested or 1)

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
