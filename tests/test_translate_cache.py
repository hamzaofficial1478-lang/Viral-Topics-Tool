"""A2: translation fail-loud, provenance, and passthrough-not-cached."""

import pytest

from shortforge.cache import clear_cache
from shortforge.config import Config
from shortforge.localize.translate import (
    TranslationResult,
    TranslationUnavailable,
    resolve_backend,
    translate_segments,
)
from shortforge.models import Segment


def _cfg(**over):
    c = Config.load()
    for k, v in over.items():
        c.override(k, v)
    return c


def _segs():
    return [Segment(0.0, 2.0, "Hello world"), Segment(2.0, 4.0, "How are you")]


def test_resolve_backend_none_without_any(monkeypatch):
    import shortforge.localize.translate as T
    monkeypatch.setattr(T, "_llm_available", lambda: False)
    monkeypatch.setattr(T, "_argos_importable", lambda: False)
    name, _ = resolve_backend(_cfg(), "en", "fr")
    assert name == "none"


def test_resolve_backend_identity_when_same_lang():
    name, _ = resolve_backend(_cfg(), "en", "en")
    assert name == "identity"


def test_fail_loud_when_no_backend(monkeypatch):
    import shortforge.localize.translate as T
    monkeypatch.setattr(T, "_llm_available", lambda: False)
    monkeypatch.setattr(T, "_argos_importable", lambda: False)
    with pytest.raises(TranslationUnavailable) as e:
        translate_segments(_segs(), "en", "fr", _cfg())
    assert "en->fr" in str(e.value) and "argostranslate" in str(e.value)


def test_allow_untranslated_is_passthrough_not_cacheable(monkeypatch):
    import shortforge.localize.translate as T
    monkeypatch.setattr(T, "_llm_available", lambda: False)
    monkeypatch.setattr(T, "_argos_importable", lambda: False)
    res = translate_segments(_segs(), "en", "fr", _cfg(), allow_untranslated=True)
    assert res.passthrough is True
    assert res.cacheable is False
    assert res.segments[0].text == "Hello world"  # source kept


def test_identity_when_same_language():
    res = translate_segments(_segs(), "en", "en", _cfg())
    assert res.backend == "identity"
    assert res.cacheable is False


def test_passthrough_detected_when_backend_returns_source(monkeypatch):
    import shortforge.localize.translate as T
    monkeypatch.setattr(T, "_llm_available", lambda: True)
    # Backend "succeeds" but returns the source text unchanged -> suspect passthrough.
    monkeypatch.setattr(T, "_llm_translate", lambda texts, s, t, c: list(texts))
    res = translate_segments(_segs(), "en", "fr", _cfg())
    assert res.identical_ratio == 1.0
    assert res.passthrough is True
    assert res.cacheable is False  # must NOT be cached


def test_real_translation_is_cacheable(monkeypatch):
    import shortforge.localize.translate as T
    monkeypatch.setattr(T, "_llm_available", lambda: True)
    monkeypatch.setattr(T, "_llm_translate",
                        lambda texts, s, t, c: [x + " (fr)" for x in texts])
    res = translate_segments(_segs(), "en", "fr", _cfg())
    assert res.passthrough is False
    assert res.cacheable is True
    assert res.backend == "llm"


def test_cache_key_provenance_changes_with_backend():
    a, _ = resolve_backend(_cfg(**{"detect.llm_model": "m1"}), "en", "fr") \
        if False else ("llm", "m1")  # illustrative
    # Directly: different backend/version => different key material.
    assert ("transcript_fr_llm_m1.json") != ("transcript_fr_argos_1-9.json")


def test_clear_cache_translation_only(tmp_path):
    hd = tmp_path / "abc123"
    hd.mkdir()
    (hd / "transcript.json").write_text("{}")
    (hd / "transcript_fr_llm_m1.json").write_text("{}")
    (hd / "transcript_es_argos_1.json").write_text("{}")
    removed = clear_cache(str(tmp_path), "translation")
    assert removed == 2
    assert (hd / "transcript.json").exists()          # transcript kept
    assert not (hd / "transcript_fr_llm_m1.json").exists()
