"""L: provider layer — capabilities, router failover, cost, caching, chunking."""

import os

import pytest

from shortforge.providers.base import Capabilities, CostTracker
from shortforge.providers.tts import (EdgeTTSProvider, OpenAICompatibleTTSProvider,
                                       ProviderError, TTSRouter, build_tts_router)
from shortforge.providers.audio_library import LocalAudioLibrary


def _clear(mp):
    for k in list(os.environ):
        if k.startswith(("TTS_", "AUDIO_LIB_", "EDGE_VOICE_")):
            mp.delenv(k, raising=False)


# --- Capabilities ----------------------------------------------------------- #

def test_capabilities_from_env(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TTS_X_LANGS", "en, de , es-ES")
    monkeypatch.setenv("TTS_X_SSML", "true")
    monkeypatch.setenv("TTS_X_EMOTION", "yes")
    monkeypatch.setenv("TTS_X_MAX_CHARS", "1200")
    monkeypatch.setenv("TTS_X_COST_PER_1K", "15")
    c = Capabilities.from_env("TTS_X")
    assert c.languages == {"en", "de", "es"}
    assert c.ssml and c.emotion and not c.cloning
    assert c.max_chars == 1200 and c.cost_per_1k_chars == 15.0
    assert c.supports_language("es-419") and not c.supports_language("ja")


def test_capabilities_any_language_when_unset():
    c = Capabilities()
    assert c.supports_language("ja") and c.supports_language(None)


# --- mock provider ---------------------------------------------------------- #

class MockTTS:
    def __init__(self, name, caps, fail=False, counter=None):
        self.name = name
        self.caps = caps
        self.fail = fail
        self.counter = counter if counter is not None else []
    def available(self):
        return True
    def voice_for(self, language):
        return f"{self.name}-{language}"
    def supports(self, *, language=None, ssml=False, emotion=False, cloning=False):
        return TTSRouter.__mro__ and _supports(self.caps, language, ssml, emotion, cloning)
    def synthesize(self, text, *, language, out_path, voice=None, ssml=False, style=None):
        self.counter.append((self.name, text))
        if self.fail:
            raise RuntimeError(f"{self.name} boom")
        with open(out_path, "wb") as f:
            f.write(b"RIFFmock")
        from shortforge.providers.base import SynthResult
        return SynthResult(out_path, self.name, len(text),
                           len(text) / 1000.0 * self.caps.cost_per_1k_chars, voice=voice)


def _supports(caps, language, ssml, emotion, cloning):
    if not caps.supports_language(language):
        return False
    if ssml and not caps.ssml:
        return False
    if emotion and not caps.emotion:
        return False
    if cloning and not caps.cloning:
        return False
    return True


def test_router_fails_over_to_next_provider(tmp_path):
    calls = []
    p1 = MockTTS("a", Capabilities(cost_per_1k_chars=10), fail=True, counter=calls)
    p2 = MockTTS("b", Capabilities(cost_per_1k_chars=5), fail=False, counter=calls)
    router = TTSRouter([p1, p2], cache_dir=str(tmp_path / "c"))
    res = router.synthesize("hello", language="en", out_path=str(tmp_path / "o.wav"))
    assert res.provider == "b"            # failed over from a -> b
    assert ("a", "hello") in calls and ("b", "hello") in calls


def test_router_tracks_cost(tmp_path):
    p = MockTTS("a", Capabilities(cost_per_1k_chars=20))
    router = TTSRouter([p], cache_dir=str(tmp_path / "c"))
    router.synthesize("x" * 1000, language="en", out_path=str(tmp_path / "o.wav"))
    assert router.cost.by_provider["a"]["chars"] == 1000
    assert abs(router.cost.total_cost() - 20.0 / 1000 * 1000) < 1e-6  # $20/1k * 1000 chars = $20


def test_router_caches_identical_lines(tmp_path):
    calls = []
    p = MockTTS("a", Capabilities(), counter=calls)
    router = TTSRouter([p], cache_dir=str(tmp_path / "c"))
    router.synthesize("same", language="en", out_path=str(tmp_path / "o1.wav"))
    res2 = router.synthesize("same", language="en", out_path=str(tmp_path / "o2.wav"))
    assert res2.cached is True
    assert len(calls) == 1                # synthesized once, second was a cache hit


def test_router_raises_when_no_provider_supports_caps(tmp_path):
    p = MockTTS("a", Capabilities(ssml=False))
    router = TTSRouter([p], cache_dir=str(tmp_path / "c"))
    with pytest.raises(ProviderError):
        router.synthesize("hi", language="en", out_path=str(tmp_path / "o.wav"), need_ssml=True)


def test_chunking_splits_long_input(tmp_path):
    calls = []
    p = MockTTS("a", Capabilities(max_chars=20), counter=calls)
    router = TTSRouter([p], cache_dir=None)
    long = "One two three. Four five six. Seven eight nine. Ten eleven twelve."
    # ffmpeg concat needs real audio; mock writes bytes, so just assert it splits.
    try:
        router.synthesize(long, language="en", out_path=str(tmp_path / "o.wav"))
    except Exception:
        pass  # concat may fail on mock bytes; we only assert the split happened
    assert len(calls) >= 3                # long input was chunked into >=3 synth calls


def test_build_router_from_env_priority(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TTS_PROVIDERS", "myvendor,edge")
    monkeypatch.setenv("TTS_MYVENDOR_API_KEY", "k")
    router = build_tts_router()
    assert [p.name for p in router.providers] == ["myvendor", "edge"]
    assert isinstance(router.providers[0], OpenAICompatibleTTSProvider)
    assert isinstance(router.providers[1], EdgeTTSProvider)


# --- audio library ---------------------------------------------------------- #

def test_local_audio_library_indexes_and_matches(tmp_path, monkeypatch):
    _clear(monkeypatch)
    music = tmp_path / "music"; music.mkdir()
    (music / "calm.wav").write_bytes(b"RIFF")
    (tmp_path / "library.json").write_text(
        '{"assets":[{"file":"calm.wav","title":"Calm","licence":"CC0","mood":"calm","energy":0.2}]}')
    lib = LocalAudioLibrary(str(tmp_path))
    assert lib.available()
    hit = lib.find(mood="calm", kind="music")
    assert hit and hit["licence"] == "CC0" and hit["kind"] == "music"
