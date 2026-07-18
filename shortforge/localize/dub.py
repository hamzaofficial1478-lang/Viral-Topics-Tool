"""M6 — Dub orchestration (Phase 3).

Ties translation + TTS + stem-preserving re-mix together, per clip. Produces a
clip-length dubbed audio track (voiceover over the preserved music/SFX bed) and
a translated transcript for captions in the target language.
"""

from __future__ import annotations

import os

from ..config import Config
from ..models import Clip, Segment, Transcript, Word
from ..utils import ShortForgeError, log
from . import stems, tts
from .translate import translate_segments


def _even_words(text: str, start: float, end: float) -> list[Word]:
    tokens = text.split()
    span = max(0.05, end - start)
    per = span / max(1, len(tokens))
    return [
        Word(start=start + i * per, end=start + (i + 1) * per, text=t)
        for i, t in enumerate(tokens)
    ]


def build_translated_transcript(transcript: Transcript, tgt: str, cfg: Config) -> Transcript:
    """Translate all segments and rebuild even word-timing for captions."""
    src = transcript.language or "en"
    translated = translate_segments(transcript.segments, src, tgt, cfg)
    segs = [
        Segment(s.start, s.end, s.text, _even_words(s.text, s.start, s.end))
        for s in translated
    ]
    return Transcript(language=tgt, duration=transcript.duration, segments=segs)


def dub_clip(
    source_path: str,
    clip: Clip,
    translated: Transcript,
    cfg: Config,
    work_dir: str,
    accompaniment_source: str | None = None,
    allow_voice_bleed: bool = False,
) -> tuple[str | None, str]:
    """Build dubbed audio for ``clip``. Returns (wav_path|None, method).

    ``accompaniment_source`` is the full-source vocals-removed WAV (from one-time
    Demucs separation); when present the original speech is gone and we mix the
    dub over a clean music/SFX bed. Without it we only proceed if
    ``allow_voice_bleed`` is set (ducking the untouched original — two voices);
    otherwise the pipeline has already aborted before reaching here.
    """
    tgt = translated.language
    clip_work = os.path.join(work_dir, f"dub_{clip.clip_id}")
    os.makedirs(clip_work, exist_ok=True)

    # Segments overlapping the clip, rebased to clip-relative time.
    rel: list[Segment] = []
    for s in translated.segments:
        if s.end > clip.start and s.start < clip.end and s.text.strip():
            rel.append(
                Segment(
                    start=max(0.0, s.start - clip.start),
                    end=min(clip.duration, s.end - clip.start),
                    text=s.text,
                )
            )
    if not rel:
        log.info("clip %s: no speech to dub", clip.clip_id)
        return None, "none"

    voice = tts.build_dub_track(rel, clip.duration, tgt, cfg, clip_work)
    if voice is None:
        return None, "none"

    if accompaniment_source:
        bed = stems.slice_audio(
            accompaniment_source, clip.start, clip.duration,
            os.path.join(clip_work, "bed.wav"),
        )
        mixed = stems.remix_ducked(voice, bed, clip_work, cfg)
        return mixed, "stems"

    if allow_voice_bleed:
        clip_audio = stems.extract_clip_audio(
            source_path, clip, os.path.join(clip_work, "orig.wav"))
        mixed = stems.remix_bleed(voice, clip_audio, clip_work, cfg)
        return mixed, "duck-bleed"

    # Should be unreachable: the pipeline aborts before dubbing when stems are
    # unavailable and bleed isn't allowed. Guard anyway rather than emit bleed.
    raise ShortForgeError(
        "Cannot remove the original voice: stem separation unavailable. Install "
        "Demucs (`pip install demucs`) or pass --allow-voice-bleed to accept two "
        "voices."
    )
