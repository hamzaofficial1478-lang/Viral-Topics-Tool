"""L — Provider interfaces + capability declaration (vendor-agnostic).

Three interfaces (`LLMProvider`, `TTSProvider`, `AudioLibraryProvider`) plus a
`Capabilities` record every TTS provider declares, so the synthesis layer can
pick a provider by *what it can do* (SSML? emotion? this language?) rather than
by name. Adding a provider means implementing an interface — nothing else changes.

Keys are never stored in logs or reprs; only redacted forms are ever printed.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, ""))
    except (TypeError, ValueError):
        return default


def redact(text: str, key: str | None) -> str:
    if key and len(key) > 6:
        return text.replace(key, key[:3] + "…" + key[-2:])
    return text


@dataclass
class Capabilities:
    """What a TTS provider can do (declared per provider, usually via env)."""
    languages: set[str] | None = None   # None = any/unknown; else ISO codes
    ssml: bool = False
    emotion: bool = False               # style / emotion parameter support
    cloning: bool = False               # voice cloning from a reference sample
    max_chars: int = 4000               # per-request character limit
    cost_per_1k_chars: float = 0.0      # USD per 1000 characters (for tracking)

    def supports_language(self, lang: str | None) -> bool:
        if not lang or self.languages is None:
            return True
        return lang.split("-")[0].lower() in self.languages

    def to_dict(self) -> dict:
        d = asdict(self)
        d["languages"] = sorted(self.languages) if self.languages else "any"
        return d

    @classmethod
    def from_env(cls, prefix: str) -> "Capabilities":
        langs_raw = _env(f"{prefix}_LANGS", "").strip()
        languages = None
        if langs_raw:
            languages = {c.strip().split("-")[0].lower() for c in langs_raw.split(",") if c.strip()}
        return cls(
            languages=languages,
            ssml=_env_bool(f"{prefix}_SSML", False),
            emotion=_env_bool(f"{prefix}_EMOTION", False),
            cloning=_env_bool(f"{prefix}_CLONE", False),
            max_chars=int(_env_float(f"{prefix}_MAX_CHARS", 4000)),
            cost_per_1k_chars=_env_float(f"{prefix}_COST_PER_1K", 0.0),
        )


@dataclass
class SynthResult:
    path: str
    provider: str
    chars: int
    cost: float
    cached: bool = False
    voice: str | None = None
    style: str | None = None


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def describe(self) -> str: ...

    @abstractmethod
    def complete_json(self, prompt: str, schema: dict, *,
                      system: str | None = None, max_tokens: int = 1200) -> dict: ...


class TTSProvider(ABC):
    name: str
    caps: Capabilities

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def voice_for(self, language: str) -> str | None: ...

    @abstractmethod
    def synthesize(self, text: str, *, language: str, out_path: str,
                   voice: str | None = None, ssml: bool = False,
                   style: dict | None = None) -> SynthResult:
        """Synthesize ``text`` to ``out_path``. Raise on failure (router fails over)."""

    def supports(self, *, language: str | None = None, ssml: bool = False,
                 emotion: bool = False, cloning: bool = False) -> bool:
        c = self.caps
        if not c.supports_language(language):
            return False
        if ssml and not c.ssml:
            return False
        if emotion and not c.emotion:
            return False
        if cloning and not c.cloning:
            return False
        return True

    def describe(self) -> str:
        return f"{self.name}: {self.caps.to_dict()}"


class ASRProvider(ABC):
    """Transcription backend: local Whisper or an API (OpenAI-compatible / Canary)."""
    name: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def transcribe(self, audio_path: str, *, language: str | None = None,
                   duration: float = 0.0):
        """Return a Transcript (segments with timing, text, confidence)."""


class AudioLibraryProvider(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def describe(self) -> str: ...

    @abstractmethod
    def find(self, *, mood: str | None = None, energy: float | None = None,
             duration: float | None = None, kind: str = "music") -> dict | None:
        """Return an asset descriptor {path/url, title, licence, source} or None."""


@dataclass
class CostTracker:
    """Per-provider char + cost accumulation for a job (written to the manifest)."""
    by_provider: dict[str, dict] = field(default_factory=dict)

    def add(self, provider: str, chars: int, cost: float) -> None:
        e = self.by_provider.setdefault(provider, {"chars": 0, "cost": 0.0, "calls": 0})
        e["chars"] += chars
        e["cost"] = round(e["cost"] + cost, 6)
        e["calls"] += 1

    def total_cost(self) -> float:
        return round(sum(e["cost"] for e in self.by_provider.values()), 6)

    def to_dict(self) -> dict:
        return {"by_provider": self.by_provider, "total_cost": self.total_cost()}
