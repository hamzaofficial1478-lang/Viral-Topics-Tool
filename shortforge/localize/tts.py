"""M6 — Text-to-speech voiceover + time alignment (Phase 3).

Backends:
  espeak : offline, robotic, every language — the reliable default/fallback.
  edge   : Microsoft Edge neural voices — natural, free, needs network.
  xtts   : voice-cloning (keeps the operator's voice) — GPU, opt-in.

The dub is built per segment and each synthesized clip is time-stretched to fit
its original segment slot, so the voiceover stays aligned to the video.
"""

from __future__ import annotations

import os

from ..config import Config
from ..utils import ShortForgeError, media_duration, require_binary, run, log
from ..models import Segment

# espeak-ng takes the bare language code.
_EDGE_VOICE = {
    "en": "en-US-AriaNeural", "de": "de-DE-KatjaNeural", "es": "es-ES-AlvaroNeural",
    "it": "it-IT-ElsaNeural", "ja": "ja-JP-NanamiNeural", "ar": "ar-SA-HamedNeural",
    "fr": "fr-FR-DeniseNeural", "pt": "pt-BR-FranciscaNeural", "hi": "hi-IN-SwaraNeural",
    "tr": "tr-TR-EmelNeural", "ru": "ru-RU-SvetlanaNeural",
}

_edge_broken = False  # once Edge fails (e.g. offline), stop retrying this run


def resolve_backend(cfg: Config) -> str:
    return str(cfg.get("localize.tts_backend", "auto")).lower()


def _synth_espeak(text: str, lang: str, out_wav: str, cfg: Config) -> str:
    espeak = require_binary("espeak-ng")
    speed = int(cfg.get("localize.espeak_speed", 165))
    run([espeak, "-v", lang.split("-")[0], "-s", str(speed), "-w", out_wav, text])
    return out_wav


def _synth_edge(text: str, lang: str, out_wav: str, cfg: Config) -> str:
    global _edge_broken
    if _edge_broken:
        raise ShortForgeError("edge-tts previously failed")
    import asyncio

    import edge_tts

    voice = cfg.get("localize.edge_voice") or _EDGE_VOICE.get(lang.split("-")[0], "en-US-AriaNeural")
    mp3 = out_wav + ".mp3"

    async def _go():
        await edge_tts.Communicate(text, voice).save(mp3)

    try:
        asyncio.run(_go())
    except Exception as e:  # noqa: BLE001
        _edge_broken = True
        raise ShortForgeError(f"edge-tts failed: {e}") from e
    ffmpeg = require_binary("ffmpeg")
    run([ffmpeg, "-y", "-i", mp3, "-ar", "48000", "-ac", "2", out_wav])
    os.remove(mp3)
    return out_wav


def synthesize(text: str, lang: str, out_wav: str, cfg: Config) -> str:
    """Synthesize one utterance to a WAV. Falls back espeak when Edge is down."""
    backend = resolve_backend(cfg)
    if backend in ("edge", "auto"):
        try:
            return _synth_edge(text, lang, out_wav, cfg)
        except ShortForgeError:
            if backend == "edge":
                raise
            # auto: fall through to espeak
    if backend == "xtts":
        return _synth_xtts(text, lang, out_wav, cfg)
    return _synth_espeak(text, lang, out_wav, cfg)


def _synth_xtts(text: str, lang: str, out_wav: str, cfg: Config) -> str:  # pragma: no cover
    # Voice cloning (GPU). Requires TTS (coqui) + a speaker reference clip.
    try:
        from TTS.api import TTS
    except ImportError as e:
        raise ShortForgeError(
            "xtts backend needs the `TTS` package + a GPU. Use --tts espeak/edge here."
        ) from e
    speaker = cfg.get("localize.voice_sample")
    if not speaker or not os.path.isfile(speaker):
        raise ShortForgeError("xtts needs localize.voice_sample = a clip of your voice")
    model = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    model.tts_to_file(text=text, speaker_wav=speaker, language=lang.split("-")[0],
                      file_path=out_wav)
    return out_wav


def _atempo_chain(speed: float) -> str:
    """Build an atempo filter chain for an arbitrary speed factor (0.33..3.0)."""
    speed = max(0.33, min(3.0, speed))
    parts = []
    # atempo only accepts 0.5..2.0 per stage; factor across stages.
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


def _fit_segment(raw_wav: str, target: float, out_wav: str) -> str:
    """Time-stretch ``raw_wav`` to ~``target`` seconds and pad/trim exactly."""
    ffmpeg = require_binary("ffmpeg")
    actual = max(0.05, media_duration(raw_wav))
    speed = actual / max(0.2, target)
    af = f"{_atempo_chain(speed)},apad,atrim=0:{target:.3f},asetpts=N/SR/TB"
    run([ffmpeg, "-y", "-i", raw_wav, "-af", af, "-ar", "48000", "-ac", "2", out_wav])
    return out_wav


def build_dub_track(
    segments_rel: list[Segment], clip_dur: float, lang: str, cfg: Config, work_dir: str
) -> str | None:
    """Synthesize + place each segment onto a clip-length voice track.

    ``segments_rel`` times are relative to the clip. Returns the WAV path, or
    None if there is nothing to voice.
    """
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(work_dir, exist_ok=True)
    fitted: list[tuple[float, str]] = []
    for i, seg in enumerate(segments_rel):
        text = seg.text.strip()
        if not text:
            continue
        target = max(0.3, seg.end - seg.start)
        raw = os.path.join(work_dir, f"seg_{i}_raw.wav")
        fit = os.path.join(work_dir, f"seg_{i}_fit.wav")
        try:
            synthesize(text, lang, raw, cfg)
        except ShortForgeError as e:
            log.warning("tts failed for segment %d (%s); skipping", i, e)
            continue
        _fit_segment(raw, target, fit)
        fitted.append((max(0.0, seg.start), fit))

    if not fitted:
        return None

    out = os.path.join(work_dir, "dub_voice.wav")
    # Base of silence sets the track length; each segment is delayed to its start.
    inputs = ["-f", "lavfi", "-t", f"{clip_dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    for _, path in fitted:
        inputs += ["-i", path]
    fc = []
    labels = ["0:a"]
    for idx, (start, _) in enumerate(fitted, start=1):
        ms = int(round(start * 1000))
        fc.append(f"[{idx}:a]adelay={ms}|{ms}[d{idx}]")
        labels.append(f"d{idx}")
    fc.append(
        "".join(f"[{l}]" for l in labels)
        + f"amix=inputs={len(labels)}:normalize=0:duration=first[out]"
    )
    run([ffmpeg, "-y", *inputs, "-filter_complex", ";".join(fc),
         "-map", "[out]", "-ar", "48000", "-ac", "2", out])
    return out
