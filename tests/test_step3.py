"""STEP 3 (ElevenLabs language coverage) + STEP 3.5 (transcription quality)."""

import json

import pytest

from shortforge.config import Config
from shortforge.models import Segment, Transcript


# --- STEP 3.5: confidence round-trip + low-confidence exclusion ------------- #

def test_segment_confidence_roundtrip():
    s = Segment(0.0, 2.0, "hi", confidence=0.87)
    d = s.to_dict()
    assert d["confidence"] == 0.87
    assert Segment.from_dict(d).confidence == 0.87
    # absent confidence stays None
    assert Segment.from_dict({"start": 0, "end": 1, "text": "x"}).confidence is None


def test_low_confidence_segment_not_anchored():
    from shortforge.detect import detect_hooks
    from shortforge.select import build_clips
    # The strongest hook is garbled/low-confidence; a big pause isolates the two
    # so coherent growth can't merge them. With min_confidence set, the clip must
    # be built around the high-confidence segment, not the garbled one.
    segs = [
        Segment(0.0, 7.0, "Did you know ninety percent of startups fail fast?", confidence=0.15),
        Segment(16.0, 23.0, "The secret to success is showing up daily every time.", confidence=0.9),
    ]
    tr = Transcript("en", 23.0, segs)
    cfg = Config.load()
    cfg.override("detect.backend", "heuristic")
    cfg.override("detect.min_segment_score", 0.0)
    cfg.override("select.num_clips", 1)
    cfg.override("select.target_duration", 7)
    cfg.override("select.tolerance", 1)
    cfg.override("transcribe.min_confidence", 0.5)
    clips = build_clips(tr, detect_hooks(tr, cfg), cfg, "h")
    assert clips
    assert "Did you know" not in clips[0].caption_text     # garbled segment excluded
    assert clips[0].caption_text.startswith("The secret")


def test_doctor_warns_on_base_model():
    from shortforge.doctor import _whisper_model, WARN, OK
    cfg = Config.load()
    cfg.override("transcribe.model", "base")
    assert _whisper_model(cfg).status == WARN
    cfg.override("transcribe.model", "small")
    assert _whisper_model(cfg).status == OK


def test_default_model_is_not_base():
    assert Config.load().get("transcribe.model") != "base"


# --- STEP 3: per-model language detection + dub-language validation --------- #

_ELEVEN_MODELS = [
    {"model_id": "eleven_monolingual_v1", "languages": [{"language_id": "en", "name": "English"}]},
    {"model_id": "eleven_multilingual_v2", "languages": [
        {"language_id": "en"}, {"language_id": "de"}, {"language_id": "es"},
        {"language_id": "it"}, {"language_id": "ja"}, {"language_id": "ar"}]},
]


def test_probe_model_languages_parses_elevenlabs(monkeypatch):
    from shortforge.providers import detect as D
    monkeypatch.setattr(D, "_get", lambda url, key, timeout=15: _ELEVEN_MODELS)
    ml = D._probe_model_languages("https://api.elevenlabs.io", "k")
    assert ml["eleven_monolingual_v1"] == ["en"]
    assert set(ml["eleven_multilingual_v2"]) == {"en", "de", "es", "it", "ja", "ar"}


def test_detect_sets_languages_from_configured_model(monkeypatch):
    from shortforge.providers import detect as D
    monkeypatch.setattr(D, "_probe_voices", lambda *a, **k: (["v1"], ["en"]))
    monkeypatch.setattr(D, "_get", lambda url, key, timeout=15: _ELEVEN_MODELS)
    res = D.detect("tts", "https://api.elevenlabs.io", "k", "eleven_multilingual_v2")
    assert "de" in res["languages"] and "ja" in res["languages"]
    assert res["model_languages"]["eleven_monolingual_v1"] == ["en"]


def _store_with_tts(caps):
    return {"providers": [{"id": "e1", "name": "ElevenLabs", "category": "tts",
                           "enabled": True, "priority": 0, "capabilities": caps}]}


def test_dub_language_unsupported_fails_with_suggestion():
    from shortforge.providers import dub_language_check
    store = _store_with_tts({"languages": ["en"],
                             "model_languages": {"eleven_multilingual_v2": ["en", "de"]}})
    ok, msg = dub_language_check(store, "de")
    assert ok is False
    assert "de" in msg and "eleven_multilingual_v2" in msg


def test_dub_language_supported_ok():
    from shortforge.providers import dub_language_check
    ok, msg = dub_language_check(_store_with_tts({"languages": ["en", "de", "es"]}), "de")
    assert ok is True and msg == ""


def test_dub_language_unknown_is_permissive():
    from shortforge.providers import dub_language_check
    ok, _ = dub_language_check(_store_with_tts({"languages": None}), "ja")
    assert ok is True


def test_dub_language_no_providers_ok():
    from shortforge.providers import dub_language_check
    ok, _ = dub_language_check({"providers": []}, "de")
    assert ok is True
