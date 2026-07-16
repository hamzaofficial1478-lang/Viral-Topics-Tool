"""Core data structures passed between pipeline modules.

Everything is a plain dataclass with ``to_dict`` / ``from_dict`` so it can be
JSON-cached (M12 caching layer) and reused across re-runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Word:
    """A single transcribed word with timing (seconds from source start)."""

    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Word":
        return cls(start=d["start"], end=d["end"], text=d["text"])


@dataclass
class Segment:
    """A sentence-ish chunk from the ASR, with its constituent words."""

    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Segment":
        return cls(
            start=d["start"],
            end=d["end"],
            text=d["text"],
            words=[Word.from_dict(w) for w in d.get("words", [])],
        )


@dataclass
class Transcript:
    """Full transcript: ordered segments + detected language + duration."""

    language: str
    duration: float
    segments: list[Segment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()

    def words(self) -> list[Word]:
        out: list[Word] = []
        for s in self.segments:
            out.extend(s.words)
        return out

    def words_in(self, start: float, end: float) -> list[Word]:
        """Words whose midpoint falls within [start, end)."""
        out: list[Word] = []
        for w in self.words():
            mid = (w.start + w.end) / 2.0
            if start <= mid < end:
                out.append(w)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "duration": self.duration,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Transcript":
        return cls(
            language=d["language"],
            duration=d["duration"],
            segments=[Segment.from_dict(s) for s in d.get("segments", [])],
        )


@dataclass
class SourceMeta:
    """Result of M1 ingestion."""

    source: str            # original URL or path the operator supplied
    file_path: str         # local media file to work from
    hash: str              # content key for caching
    title: str = ""
    description: str = ""
    duration: float = 0.0
    src_lang: str = ""
    uploader: str = ""
    video_id: str = ""
    owner_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceMeta":
        return cls(**d)


@dataclass
class Candidate:
    """A scored highlight from M3, before it is turned into a concrete clip."""

    start: float
    end: float
    score: float           # 0..1
    reason: str = ""
    signals: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Candidate":
        return cls(
            start=d["start"],
            end=d["end"],
            score=d["score"],
            reason=d.get("reason", ""),
            signals=d.get("signals", {}),
        )


@dataclass
class Clip:
    """A concrete clip to render (M4 output, consumed by M5/M7/M13)."""

    clip_id: str
    source_hash: str
    start: float
    end: float
    score: float
    reason: str = ""
    caption_text: str = ""
    resolution: str = ""
    file_path: str = ""     # filled in by the render step

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["duration"] = round(self.duration, 3)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Clip":
        return cls(
            clip_id=d["clip_id"],
            source_hash=d["source_hash"],
            start=d["start"],
            end=d["end"],
            score=d["score"],
            reason=d.get("reason", ""),
            caption_text=d.get("caption_text", ""),
            resolution=d.get("resolution", ""),
            file_path=d.get("file_path", ""),
        )
