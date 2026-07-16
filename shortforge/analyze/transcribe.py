"""M2 — Transcription with word-level timestamps (faster-whisper).

Word timing is required downstream for karaoke-ready captions and for cutting
clips on clean word/sentence boundaries. Results are cached by source hash so
every later variant (different clip count, aspect, language) reuses this pass —
the single biggest time/cost saver at scale (gap #12).
"""

from __future__ import annotations

import os
import re

from ..cache import Cache
from ..config import Config
from ..models import Segment, Transcript, Word
from ..utils import ShortForgeError, require_binary, run, log

_CACHE_NAME = "transcript.json"


def extract_audio(video_path: str, out_wav: str) -> str:
    """Extract mono 16 kHz WAV — the input ASR expects."""
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(os.path.dirname(out_wav) or ".", exist_ok=True)
    run(
        [
            ffmpeg,
            "-y",
            "-i",
            video_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            out_wav,
        ]
    )
    return out_wav


def _resolve_device(device: str, compute_type: str) -> tuple[str, str]:
    if device == "auto":
        device = "cpu"
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                device = "cuda"
        except Exception:  # pragma: no cover - environment dependent
            device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


def transcribe(meta, cfg: Config, cache: Cache) -> Transcript:
    """Transcribe ``meta.file_path`` to a :class:`Transcript` (cached)."""
    cached = cache.load_json(_CACHE_NAME)
    if cached is not None:
        log.info("using cached transcript (%d segments)", len(cached.get("segments", [])))
        return Transcript.from_dict(cached)

    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover - dependency guard
        raise ShortForgeError(
            "faster-whisper is not installed. `pip install faster-whisper`, or "
            "supply your own transcript with --transcript file.(srt|json)."
        ) from e

    model_size = cfg.get("transcribe.model", "base")
    device, compute_type = _resolve_device(
        cfg.get("transcribe.device", "auto"),
        cfg.get("transcribe.compute_type", "auto"),
    )
    language = cfg.get("transcribe.language")

    wav = extract_audio(meta.file_path, cache.path("audio16k.wav"))

    log.info("transcribing with whisper '%s' on %s/%s ...", model_size, device, compute_type)
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    seg_iter, info = model.transcribe(
        wav,
        word_timestamps=True,
        language=language,
        vad_filter=True,
    )

    segments: list[Segment] = []
    for s in seg_iter:
        words = [
            Word(start=float(w.start), end=float(w.end), text=w.word.strip())
            for w in (s.words or [])
            if w.word and w.word.strip()
        ]
        seg = Segment(
            start=float(s.start),
            end=float(s.end),
            text=s.text.strip(),
            words=words,
        )
        if seg.text:
            segments.append(seg)

    transcript = Transcript(
        language=info.language or (language or "en"),
        duration=float(getattr(info, "duration", 0.0) or meta.duration),
        segments=segments,
    )
    cache.save_json(_CACHE_NAME, transcript.to_dict())
    log.info(
        "transcript: %d segments, %d words, lang=%s",
        len(segments),
        len(transcript.words()),
        transcript.language,
    )
    return transcript


# --------------------------------------------------------------------------- #
# External transcript support (M7: "upload my own SRT")
# --------------------------------------------------------------------------- #

def load_external_transcript(path: str, duration: float, language: str = "en") -> Transcript:
    """Load an operator-supplied transcript.

    Accepts either an SRT file or a ShortForge transcript JSON. SRT lines have
    only line-level timing, so word timings are synthesized by splitting each
    line's span evenly across its words (good enough for burned captions).
    """
    import json

    if not os.path.isfile(path):
        raise ShortForgeError(f"Transcript file not found: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, "r", encoding="utf-8") as f:
            return Transcript.from_dict(json.load(f))
    if ext in (".srt", ".vtt"):
        return _parse_srt(path, duration, language)
    raise ShortForgeError(f"Unsupported transcript format: {ext} (use .srt or .json)")


_TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})")


def _ts(text: str) -> float | None:
    m = _TS_RE.search(text)
    if not m:
        return None
    h, mm, ss, ms = m.groups()
    return int(h) * 3600 + int(mm) * 60 + int(ss) + int(ms.ljust(3, "0")) / 1000.0


def _parse_srt(path: str, duration: float, language: str) -> Transcript:
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = f.read()

    blocks = re.split(r"\n\s*\n", raw.strip())
    segments: list[Segment] = []
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        time_line = next((ln for ln in lines if "-->" in ln), None)
        if not time_line:
            continue
        left, _, right = time_line.partition("-->")
        start, end = _ts(left), _ts(right)
        if start is None or end is None or end <= start:
            continue
        text_lines = [ln for ln in lines if "-->" not in ln and not ln.strip().isdigit()]
        text = " ".join(text_lines).strip()
        if not text:
            continue
        segments.append(_segment_with_even_words(start, end, text))

    if not segments:
        raise ShortForgeError(f"No usable cues parsed from {path}")
    return Transcript(language=language, duration=duration or segments[-1].end, segments=segments)


def _segment_with_even_words(start: float, end: float, text: str) -> Segment:
    tokens = text.split()
    span = max(0.01, end - start)
    per = span / max(1, len(tokens))
    words = [
        Word(start=start + i * per, end=start + (i + 1) * per, text=tok)
        for i, tok in enumerate(tokens)
    ]
    return Segment(start=start, end=end, text=text, words=words)
