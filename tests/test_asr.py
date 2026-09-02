"""STEP 3.5b: pluggable ASR — providers, dispatch, detection, transcribe benchmark."""

import pytest

from shortforge import benchmark as B
from shortforge.config import Config
from shortforge.models import Segment, Transcript
from shortforge.providers import asr as A
from shortforge.providers import detect as D
from shortforge.providers import store as S


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))


# --------------------------------------------------------------------------- #
# confidence + multipart helpers
# --------------------------------------------------------------------------- #

def test_confidence_maps_logprob_and_no_speech():
    # avg_logprob 0 -> exp(0)=1; no_speech 0 -> full confidence.
    assert A._confidence(0.0, 0.0) == 1.0
    # very negative logprob -> near zero.
    assert A._confidence(-5.0, 0.0) < 0.1
    # high no_speech probability suppresses confidence.
    assert A._confidence(0.0, 1.0) == 0.0
    # missing logprob -> unknown.
    assert A._confidence(None, 0.0) is None


def test_multipart_shape(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"RIFFxxxx")
    body, ctype = A._multipart({"model": "whisper-1", "response_format": "verbose_json"},
                               "file", str(f))
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'name="file"; filename="a.wav"' in body
    assert b"RIFFxxxx" in body


# --------------------------------------------------------------------------- #
# backend construction
# --------------------------------------------------------------------------- #

def test_local_whisper_reads_model_size():
    cfg = Config.load()
    cfg.override("transcribe.model", "medium")
    asr = A.LocalWhisperASR(cfg)
    assert asr.name == "local-whisper" and asr.model_size == "medium"


def test_openai_asr_url_is_version_aware():
    a = A.OpenAICompatibleASR("x", base_url="https://api.example.com/v1", api_key="k", model="whisper-1")
    assert a._url() == "https://api.example.com/v1/audio/transcriptions"
    b = A.OpenAICompatibleASR("y", base_url="https://api.example.com", api_key="k", model="whisper-1")
    assert b._url() == "https://api.example.com/v1/audio/transcriptions"          # no doubled /v1


def test_openai_asr_from_config_reads_cost_and_availability():
    a = A.OpenAICompatibleASR.from_config(
        {"name": "canary", "base_url": "https://x/v1", "api_key": "k", "model": "canary-1b",
         "capabilities": {"cost_per_min": 0.006}})
    assert a.name == "canary" and a.model == "canary-1b" and a.cost_per_min == 0.006
    assert a.available() is True
    assert A.OpenAICompatibleASR("z", base_url="", api_key="", model="").available() is False


# --------------------------------------------------------------------------- #
# provider selection / dispatch
# --------------------------------------------------------------------------- #

def test_local_whisper_is_always_the_fallback():
    cfg = Config.load()
    provs = A.list_asr_providers(cfg)                       # empty store
    assert provs and isinstance(provs[-1], A.LocalWhisperASR)


def test_build_prefers_available_store_provider():
    store = {"providers": []}
    p = S.add_provider(store, name="canary-api", category="asr",
                       base_url="https://integrate.api.nvidia.com/v1",
                       api_key="nvapi-secret", model="nvidia/canary-1b")
    S.save_store(store)
    cfg = Config.load()
    chosen = A.build_asr_provider(cfg)
    assert isinstance(chosen, A.OpenAICompatibleASR)
    assert chosen.name == "canary-api" and chosen.model == "nvidia/canary-1b"


def test_build_falls_back_to_local_when_store_empty():
    cfg = Config.load()
    assert isinstance(A.build_asr_provider(cfg), A.LocalWhisperASR)


def test_local_store_entry_without_key_becomes_local_backend():
    store = {"providers": []}
    S.add_provider(store, name="local whisper", category="asr")     # no api_key
    S.save_store(store)
    cfg = Config.load()
    provs = A.list_asr_providers(cfg)
    assert all(isinstance(p, A.LocalWhisperASR) for p in provs)


# --------------------------------------------------------------------------- #
# detection — keyword filtering (network stubbed)
# --------------------------------------------------------------------------- #

def test_detect_asr_flags_canary_and_whisper(monkeypatch):
    monkeypatch.setattr(D, "_probe_models",
                        lambda url, key: ["nvidia/canary-1b", "whisper-large-v3",
                                          "minimaxai/minimax-m3", "gpt-4o"])
    res = D.detect("asr", "https://integrate.api.nvidia.com/v1", "nvapi-key")
    assert res["reachable"] is True
    assert set(res["asr_models"]) == {"nvidia/canary-1b", "whisper-large-v3"}
    assert any("Canary" in n for n in res["notes"])


def test_detect_asr_notes_when_no_asr_model(monkeypatch):
    monkeypatch.setattr(D, "_probe_models", lambda url, key: ["gpt-4o", "minimaxai/minimax-m3"])
    res = D.detect("asr", "https://x/v1", "k")
    assert res["asr_models"] == []
    assert any("different" in n or "audio/transcriptions" in n for n in res["notes"])


def test_asr_is_a_store_category():
    assert "asr" in S.CATEGORIES
    assert "transcription" in S.category_label("asr").lower()


# --------------------------------------------------------------------------- #
# transcribe benchmark
# --------------------------------------------------------------------------- #

class _FakeASR:
    def __init__(self, name, text, conf, cost_per_min=0.0, boom=False):
        self.name = name
        self.model = name + "-model"
        self._text = text
        self._conf = conf
        self.cost_per_min = cost_per_min
        self._boom = boom

    def available(self):
        return True

    def transcribe(self, audio_path, *, language=None, duration=0.0):
        if self._boom:
            raise RuntimeError("endpoint unreachable")
        seg = Segment(0.0, duration or 5.0, self._text, confidence=self._conf)
        return Transcript(language or "fr", duration or 5.0, [seg])


def test_run_transcribe_ranks_and_measures(monkeypatch):
    backends = [
        _FakeASR("clean", "les histoires de pirates les plus chargees", 0.9),
        _FakeASR("garbled", "plus bustiers une part chacun voila", 0.2),
        _FakeASR("dead", "", 0.0, boom=True),
    ]
    monkeypatch.setattr(B, "_asr_backends", lambda cfg: backends)
    res = B.run_transcribe("/x/audio.wav", Config.load(), language="fr", duration=60.0)

    assert res["task"] == "transcribe"
    rows = {r["provider"]: r for r in res["backends"]}
    # reference = highest-confidence successful backend
    assert res["reference"] == "clean"
    assert rows["clean"]["divergence_pct"] == 0.0
    assert rows["garbled"]["divergence_pct"] > 0            # differs from reference
    assert rows["garbled"]["low_conf"] == 1                 # 0.2 < 0.35
    assert rows["clean"]["cost"] is None                    # API backend, no rate -> unknown
    assert rows["dead"]["error"] and rows["dead"]["divergence_pct"] is None
    assert all("latency_ms" in r for r in res["backends"])


def test_run_transcribe_markdown_and_cost(monkeypatch):
    backends = [_FakeASR("api", "bonjour le monde", 0.8, cost_per_min=0.006)]
    monkeypatch.setattr(B, "_asr_backends", lambda cfg: backends)
    res = B.run_transcribe("/x/a.wav", Config.load(), language="fr", duration=120.0)
    assert res["backends"][0]["cost"] == round(2.0 * 0.006, 4)     # 120s = 2 min
    md = B.render_markdown(res)
    assert "ASR benchmark" in md and "Divergence" in md and "bonjour le monde" in md


# --- an untimed transcript must not become one whole-file segment ------------ #
# Some /audio/transcriptions endpoints answer with `text` only, even for
# verbose_json. Wrapping that in a single Segment(0, duration) reached the
# selector as one unsplittable block — the "asked for 2 minutes, got 15" bug.

def test_text_only_response_is_spread_into_usable_segments():
    from shortforge.providers.asr import _spread_text

    text = ("First thing happens here. Second thing happens next. Third thing "
            "follows on. Fourth thing wraps it up. Fifth thing is the payoff.")
    segs = _spread_text(text, duration=120.0, target=20.0)

    assert len(segs) > 1                                  # not one giant block
    assert segs[0].start == 0.0
    assert segs[-1].end == pytest.approx(120.0)
    for a, b in zip(segs, segs[1:]):                      # contiguous, in order
        assert b.start == pytest.approx(a.end)
    joined = " ".join(s.text for s in segs)
    for word in text.split():
        assert word in joined                             # nothing dropped


def test_spread_text_gives_words_timings_for_captions():
    from shortforge.providers.asr import _spread_text

    segs = _spread_text("alpha beta gamma delta.", duration=8.0, target=20.0)
    words = [w for s in segs for w in s.words]
    assert [w.text for w in words] == ["alpha", "beta", "gamma", "delta."]
    assert words[0].start == 0.0 and words[-1].end == pytest.approx(8.0)
    for a, b in zip(words, words[1:]):
        assert b.start >= a.start


def test_spread_text_handles_empty_and_unknown_duration():
    from shortforge.providers.asr import _spread_text

    assert _spread_text("", 60.0) == []
    assert len(_spread_text("some text with no duration", 0.0)) == 1
