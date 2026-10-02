"""BUG1 — shared URL normalization (no /v1/v1 or /chat/completions doubling).

One normalizer for the probe AND production, for credential AND legacy entries.
"""

import pytest

from shortforge.llm import normalize_base_url, normalize_api_url, normalize_chat_url
from shortforge.providers import store as S


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))


@pytest.mark.parametrize("raw,expected", [
    ("https://integrate.api.nvidia.com/v1", "https://integrate.api.nvidia.com/v1/chat/completions"),
    ("https://integrate.api.nvidia.com/v1/", "https://integrate.api.nvidia.com/v1/chat/completions"),
    ("https://integrate.api.nvidia.com/v1/chat/completions",
     "https://integrate.api.nvidia.com/v1/chat/completions"),                 # no doubling
    ("https://integrate.api.nvidia.com", "https://integrate.api.nvidia.com/v1/chat/completions"),
    ("https://agentrouter.org/v1", "https://agentrouter.org/v1/chat/completions"),
])
def test_chat_url_never_doubles(raw, expected):
    assert normalize_chat_url(raw) == expected


def test_base_strips_pasted_paths_idempotently():
    b = "https://integrate.api.nvidia.com/v1"
    assert normalize_base_url(b) == b
    assert normalize_base_url(b + "/chat/completions") == b
    assert normalize_base_url(b + "/audio/transcriptions") == b
    assert normalize_base_url(b + "/audio/speech") == b
    assert normalize_base_url(b + "/models") == b
    assert normalize_base_url(normalize_base_url(b + "/chat/completions")) == b   # idempotent


def test_api_url_for_audio_paths_no_double():
    b = "https://integrate.api.nvidia.com/v1/audio/transcriptions"   # full endpoint pasted
    assert normalize_api_url(b, "/audio/transcriptions") == \
        "https://integrate.api.nvidia.com/v1/audio/transcriptions"
    assert normalize_api_url("https://api.host.com", "/audio/speech") == \
        "https://api.host.com/v1/audio/speech"


def test_asr_and_tts_url_builders_share_normalizer():
    from shortforge.providers.asr import OpenAICompatibleASR
    a = OpenAICompatibleASR("x", base_url="https://integrate.api.nvidia.com/v1/chat/completions",
                            api_key="k", model="whisper")
    assert a._url() == "https://integrate.api.nvidia.com/v1/audio/transcriptions"   # not doubled


def test_store_normalizes_base_on_save():
    store = {"providers": [], "credentials": []}
    cred = S.add_credential(store, name="NV",
                            base_url="https://integrate.api.nvidia.com/v1/chat/completions",
                            api_key="k")
    assert cred["base_url"] == "https://integrate.api.nvidia.com/v1"          # path stripped
    p = S.add_provider(store, name="legacy", category="llm",
                       base_url="https://x/v1/chat/completions", api_key="k")
    assert p["base_url"] == "https://x/v1"
    S.update_credential(store, cred["id"], base_url="https://y/v1/audio/speech")
    assert S.get_credential(store, cred["id"])["base_url"] == "https://y/v1"
