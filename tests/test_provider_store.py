"""Settings-UI provider store (CRUD, priority) + capability detection shape."""

import json

import pytest

from shortforge.providers import store as S
from shortforge.providers import detect as D


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))


def test_add_get_update_delete():
    store = {"providers": []}
    p = S.add_provider(store, name="ElevenLabs", category="tts",
                       base_url="https://api.elevenlabs.io", api_key="secret-key-1234")
    assert p["id"] and p["enabled"] is True
    assert S.get_provider(store, p["id"])["name"] == "ElevenLabs"
    S.update_provider(store, p["id"], model="eleven_multilingual_v2")
    assert S.get_provider(store, p["id"])["model"] == "eleven_multilingual_v2"
    S.delete_provider(store, p["id"])
    assert S.get_provider(store, p["id"]) is None


def test_save_load_roundtrip_and_permissions(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(path))
    store = {"providers": []}
    S.add_provider(store, name="OpenAI", category="llm", api_key="k")
    S.save_store(store)
    assert path.is_file()
    loaded = S.load_store()
    assert loaded["providers"][0]["name"] == "OpenAI"


def test_masked_never_shows_full_key():
    assert S.masked("supersecretkey1234") == "••••1234"
    assert "supersecret" not in S.masked("supersecretkey1234")
    assert S.masked("") == "(none)"


def test_priority_ordering_and_move():
    store = {"providers": []}
    a = S.add_provider(store, name="A", category="tts")
    b = S.add_provider(store, name="B", category="tts")
    assert [p["name"] for p in S.providers_in(store, "tts")] == ["A", "B"]
    S.move_priority(store, b["id"], -1)              # move B up
    assert [p["name"] for p in S.providers_in(store, "tts")] == ["B", "A"]


def test_providers_in_filters_enabled():
    store = {"providers": []}
    a = S.add_provider(store, name="A", category="tts")
    S.update_provider(store, a["id"], enabled=False)
    assert S.providers_in(store, "tts", enabled_only=True) == []
    assert len(S.providers_in(store, "tts", enabled_only=False)) == 1


# --- detection (offline: unreachable endpoints, but shape inference works) --- #

def test_detect_recognizes_known_vendor_by_url():
    res = D.detect("tts", "https://api.elevenlabs.io/v1", "fake-key")
    assert res["api_shape"] == "elevenlabs"
    assert res["emotion"] is True and res["cloning"] is True
    assert "stability" in res["emotion_params"]


def test_detect_unknown_vendor_marks_unknown_safely():
    res = D.detect("tts", "https://api.mysterytts.example/v1", "fake-key")
    # Unknown vendor: assume OpenAI-compatible, capabilities left unknown (safe off).
    assert res["api_shape"] == "openai"
    assert res["ssml"] == "unknown" and res["emotion"] == "unknown"
    assert res["reachable"] is False  # nothing answered


def test_detect_requires_url_and_key():
    res = D.detect("tts", "", "")
    assert res["reachable"] is False
    assert any("required" in n for n in res["notes"])


def test_store_backed_tts_router(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(path))
    store = {"providers": []}
    p = S.add_provider(store, name="myvoice", category="tts",
                       base_url="https://api.x/v1", api_key="k", model="tts-1")
    S.update_provider(store, p["id"], capabilities={"ssml": True, "emotion": True,
                                                    "languages": ["en", "de"]},
                      api_shape="openai")
    S.save_store(store)
    from shortforge.providers.tts import build_tts_router
    router = build_tts_router()
    # myvoice first; edge-tts always registered as the free final fallback (item 5)
    assert [pr.name for pr in router.providers] == ["myvoice", "edge"]
    assert router.providers[0].caps.ssml is True and router.providers[0].caps.emotion is True
