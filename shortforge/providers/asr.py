"""STEP 3.5b — pluggable ASR backends.

- ``LocalWhisperASR``: faster-whisper on the operator's machine (configurable
  model size). The default.
- ``OpenAICompatibleASR``: an API transcription endpoint (OpenAI
  /audio/transcriptions shape; also fits gateways that expose Whisper/Canary
  this way) — offloads transcription from the CPU.

Both return a :class:`Transcript` with per-segment confidence, so the rest of the
pipeline is unchanged. The transcription step picks a backend from the settings
store (category ``asr``), falling back to local Whisper.
"""

from __future__ import annotations

import io
import json
import math
import re
import os
import urllib.request
import uuid

from ..config import Config
from ..models import Segment, Transcript, Word
from ..utils import log
from .base import ASRProvider


_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")


def _spread_text(text: str, duration: float, target: float = 8.0) -> list[Segment]:
    """Turn an untimed transcript into sentence-sized segments over ``duration``.

    A single whole-file segment cannot be cut into clips of a requested length,
    so an ASR that gives no timings gets approximate ones: sentences, grouped to
    roughly ``target`` seconds each, with each segment's span proportional to its
    share of the characters. Rough, but it makes the transcript usable — and the
    caller logs that the boundaries are approximate.
    """
    text = (text or "").strip()
    if not text:
        return []
    if duration <= 0:
        return [Segment(0.0, 0.0, text)]

    sentences = [s.strip() for s in _SENTENCE_END.split(text) if s.strip()] or [text]
    total = sum(len(s) for s in sentences) or 1
    # Group sentences until each chunk is about `target` seconds' worth of text.
    per_char = duration / total
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        buf = f"{buf} {s}".strip() if buf else s
        if len(buf) * per_char >= target:
            chunks.append(buf)
            buf = ""
    if buf:
        chunks.append(buf)

    out: list[Segment] = []
    t = 0.0
    for i, chunk in enumerate(chunks):
        span = duration - t if i == len(chunks) - 1 else len(chunk) * per_char
        end = min(duration, t + span)
        out.append(Segment(start=round(t, 3), end=round(end, 3), text=chunk,
                           words=_even_words(chunk, t, end)))
        t = end
    return out


def _even_words(text: str, start: float, end: float) -> list[Word]:
    """Word timings spread evenly across a span — enough for karaoke captions."""
    parts = text.split()
    if not parts or end <= start:
        return []
    step = (end - start) / len(parts)
    return [Word(round(start + i * step, 3), round(start + (i + 1) * step, 3), w)
            for i, w in enumerate(parts)]


def _confidence(avg_logprob, no_speech_prob) -> float | None:
    if avg_logprob is None:
        return None
    conf = max(0.0, min(1.0, math.exp(float(avg_logprob)))) * (1.0 - min(1.0, float(no_speech_prob or 0.0)))
    return round(conf, 3)


class LocalWhisperASR(ASRProvider):
    name = "local-whisper"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model_size = str(cfg.get("transcribe.model", "small"))

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def transcribe(self, audio_path, *, language=None, duration=0.0) -> Transcript:
        from faster_whisper import WhisperModel
        from ..analyze.transcribe import _resolve_device

        device, compute_type = _resolve_device(
            self.cfg.get("transcribe.device", "auto"),
            self.cfg.get("transcribe.compute_type", "auto"))
        log.info("ASR: local faster-whisper '%s' on %s/%s", self.model_size, device, compute_type)
        model = WhisperModel(self.model_size, device=device, compute_type=compute_type)
        seg_iter, info = model.transcribe(audio_path, word_timestamps=True,
                                          language=language, vad_filter=True)
        segments = []
        for s in seg_iter:
            words = [Word(float(w.start), float(w.end), w.word.strip())
                     for w in (s.words or []) if w.word and w.word.strip()]
            seg = Segment(float(s.start), float(s.end), s.text.strip(), words,
                          confidence=_confidence(getattr(s, "avg_logprob", None),
                                                 getattr(s, "no_speech_prob", 0.0)))
            if seg.text:
                segments.append(seg)
        return Transcript(language=info.language or (language or "en"),
                          duration=float(getattr(info, "duration", 0.0) or duration),
                          segments=segments)


def _multipart(fields: dict, file_field: str, file_path: str) -> tuple[bytes, str]:
    boundary = "----shortforge" + uuid.uuid4().hex
    buf = io.BytesIO()

    def w(s):
        buf.write(s.encode("utf-8") if isinstance(s, str) else s)

    for k, v in fields.items():
        w(f"--{boundary}\r\n")
        w(f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
    with open(file_path, "rb") as f:
        data = f.read()
    w(f"--{boundary}\r\n")
    w(f'Content-Disposition: form-data; name="{file_field}"; filename="{os.path.basename(file_path)}"\r\n')
    w("Content-Type: audio/wav\r\n\r\n")
    w(data)
    w(f"\r\n--{boundary}--\r\n")
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


class OpenAICompatibleASR(ASRProvider):
    def __init__(self, name: str, *, base_url: str, api_key: str, model: str,
                 cost_per_min: float = 0.0):
        self.name = name
        self.base_url = base_url or "https://api.openai.com/v1"
        self.api_key = api_key
        self.model = model or "whisper-1"
        self.cost_per_min = cost_per_min          # USD/min, for benchmark cost display

    @classmethod
    def from_config(cls, cfg: dict) -> "OpenAICompatibleASR":
        caps = cfg.get("capabilities") or {}
        try:
            rate = float(caps.get("cost_per_min", 0.0) or 0.0)
        except (TypeError, ValueError):
            rate = 0.0
        return cls(cfg.get("name", "asr-api"), base_url=cfg.get("base_url", ""),
                   api_key=cfg.get("api_key", ""), model=cfg.get("model", ""),
                   cost_per_min=rate)

    def available(self) -> bool:
        return bool(self.api_key)

    def _url(self) -> str:
        from ..llm import normalize_api_url
        return normalize_api_url(self.base_url, "/audio/transcriptions")

    def transcribe(self, audio_path, *, language=None, duration=0.0) -> Transcript:
        fields = {"model": self.model, "response_format": "verbose_json"}
        if language:
            fields["language"] = language
        body, ctype = _multipart(fields, "file", audio_path)
        from ..llm import bearer_header as _bearer
        req = urllib.request.Request(
            self._url(), data=body, method="POST",
            headers={**_bearer(self.api_key), "Content-Type": ctype})
        log.info("ASR: %s API transcription (%s)", self.name, self.model)
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        segments = []
        for s in data.get("segments", []):
            words = []
            for w in s.get("words", []) or data.get("words", []):
                if isinstance(w, dict) and w.get("word"):
                    words.append(Word(float(w.get("start", 0)), float(w.get("end", 0)),
                                      str(w["word"]).strip()))
            segments.append(Segment(
                float(s.get("start", 0)), float(s.get("end", 0)), str(s.get("text", "")).strip(),
                words, confidence=_confidence(s.get("avg_logprob"), s.get("no_speech_prob"))))
        if not segments and data.get("text"):
            # Some endpoints answer with `text` only (no `segments`), even for
            # verbose_json. One segment spanning the whole file then reaches the
            # selector as a single unsplittable block, which is how a request for
            # 2-minute clips produced one 15-minute clip. Spread the text over
            # sentence-sized pseudo-segments instead; timings are approximate and
            # said so in the log, but the shape is usable.
            segments = _spread_text(data["text"].strip(), duration or 0.0)
            log.warning("ASR: %s returned no per-segment timings — text was spread "
                        "evenly over %.0fs, so clip boundaries are approximate. "
                        "A backend with segment timings gives better cuts.",
                        self.name, duration or 0.0)
        return Transcript(language=data.get("language") or (language or "en"),
                          duration=duration, segments=segments)


def list_asr_providers(cfg: Config) -> list[ASRProvider]:
    """Enabled ASR providers from the store (priority order); local Whisper always last."""
    providers: list[ASRProvider] = []
    try:
        from .store import load_store, models_in
        for p in models_in(load_store(), "asr", enabled_only=True):
            name = p.get("name", "").lower()
            if "local" in name or "whisper" in name and not p.get("api_key"):
                providers.append(LocalWhisperASR(cfg))
            else:
                providers.append(OpenAICompatibleASR.from_config(p))
    except Exception as e:  # noqa: BLE001
        log.warning("could not read ASR providers (%s)", e)
    providers.append(LocalWhisperASR(cfg))   # always available as the fallback
    return providers


def build_asr_provider(cfg: Config) -> ASRProvider:
    """The ASR backend the pipeline uses: first available store provider, else local."""
    for p in list_asr_providers(cfg):
        if p.available():
            return p
    return LocalWhisperASR(cfg)
