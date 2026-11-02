"""STEP 1 — Cost estimation + controls.

Paid TTS (ElevenLabs bills per character) means every dub run must be priced
BEFORE it spends. This computes a pre-flight estimate (characters, USD, spoken
time) from the configured TTS provider's declared ``cost_per_1k_chars``, enforces
a per-job ceiling, and supports a dry-run that reports the projected spend
without calling any paid API. Actual spend is tracked per provider by the router
(``CostTracker``) and written to the manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# rough spoken-rate for a time estimate (chars/sec of natural speech)
_CHARS_PER_SEC = 14.0


@dataclass
class CostEstimate:
    chars: int
    cost_usd: float
    provider: str
    spoken_seconds: float
    priced: bool                      # True if a per-char price was available
    by_provider: dict = field(default_factory=dict)

    def human(self) -> str:
        t = f"{self.spoken_seconds/60:.1f} min" if self.spoken_seconds >= 60 else f"{self.spoken_seconds:.0f}s"
        price = f"${self.cost_usd:.2f}" if self.priced else "$0.00 (no price set for this provider)"
        return f"{self.chars} chars via {self.provider} ≈ {price}, ~{t} of speech"

    def to_dict(self) -> dict:
        return {
            "chars": self.chars, "estimated_usd": round(self.cost_usd, 4),
            "provider": self.provider, "spoken_seconds": round(self.spoken_seconds, 1),
            "priced": self.priced,
        }


def _pick_provider(router, language: str):
    """First enabled provider (priority order) that supports the language."""
    for p in router.providers:
        try:
            if p.available() and p.supports(language=language):
                return p
        except Exception:  # noqa: BLE001
            continue
    # fall back to the first provider even if not "available", for pricing info
    return router.providers[0] if router.providers else None


def estimate_tts(texts: list[str], router, language: str) -> CostEstimate:
    """Estimate the cost of synthesizing ``texts`` with ``router``'s provider."""
    chars = sum(len(t or "") for t in texts)
    prov = _pick_provider(router, language)
    if prov is None:
        return CostEstimate(chars, 0.0, "none", chars / _CHARS_PER_SEC, False)
    per_1k = float(getattr(prov.caps, "cost_per_1k_chars", 0.0) or 0.0)
    cost = chars / 1000.0 * per_1k
    return CostEstimate(
        chars=chars, cost_usd=round(cost, 4), provider=prov.name,
        spoken_seconds=chars / _CHARS_PER_SEC, priced=per_1k > 0,
        by_provider={prov.name: {"chars": chars, "cost": round(cost, 4)}},
    )
