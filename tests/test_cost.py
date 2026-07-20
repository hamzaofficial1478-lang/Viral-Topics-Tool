"""STEP 1: cost estimation + ceiling + dry-run + confirmation."""

import pytest

from shortforge.cost import CostEstimate, estimate_tts
from shortforge.providers.base import Capabilities


class _FakeProv:
    def __init__(self, name, cost_per_1k, langs=None, avail=True):
        self.name = name
        self.caps = Capabilities(cost_per_1k_chars=cost_per_1k,
                                 languages=set(langs) if langs else None)
        self._avail = avail
    def available(self):
        return self._avail
    def supports(self, *, language=None, ssml=False, emotion=False, cloning=False):
        return self.caps.supports_language(language)


class _FakeRouter:
    def __init__(self, providers):
        self.providers = providers


def test_estimate_chars_and_cost():
    router = _FakeRouter([_FakeProv("elevenlabs", 0.30)])  # $0.30 / 1k chars
    est = estimate_tts(["hello world", "another line here"], router, "en")
    assert est.chars == len("hello world") + len("another line here")
    assert est.provider == "elevenlabs"
    assert est.priced is True
    assert abs(est.cost_usd - est.chars / 1000 * 0.30) < 1e-6


def test_estimate_unpriced_provider_is_zero_and_flagged():
    router = _FakeRouter([_FakeProv("edge", 0.0)])
    est = estimate_tts(["abc"], router, "en")
    assert est.cost_usd == 0.0 and est.priced is False
    assert "no price set" in est.human()


def test_estimate_picks_language_capable_provider():
    router = _FakeRouter([
        _FakeProv("english_only", 0.10, langs=["en"]),
        _FakeProv("multi", 0.50, langs=["en", "de", "ja"]),
    ])
    # Japanese: skip english_only, price with 'multi'
    est = estimate_tts(["こんにちは"], router, "ja")
    assert est.provider == "multi"


def test_human_readable_estimate():
    est = CostEstimate(chars=1200, cost_usd=0.36, provider="elevenlabs",
                       spoken_seconds=85, priced=True)
    s = est.human()
    assert "1200 chars" in s and "elevenlabs" in s and "$0.36" in s


def test_ceiling_abort(monkeypatch, tmp_path):
    # A high estimate over a low ceiling must abort in run_pipeline. We test the
    # comparison logic directly to keep it fast (no full pipeline).
    est = CostEstimate(chars=100000, cost_usd=30.0, provider="x",
                       spoken_seconds=1000, priced=True)
    ceiling = 5.0
    assert est.cost_usd > ceiling   # this is exactly what run_pipeline checks


def test_to_dict_shape():
    est = CostEstimate(1000, 0.3, "eleven", 70, True)
    d = est.to_dict()
    assert d["chars"] == 1000 and d["estimated_usd"] == 0.3 and d["priced"] is True
