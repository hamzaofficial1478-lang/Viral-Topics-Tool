"""R6 — multi-model-per-credential schema: CRUD, flatten, priority, back-compat."""

import pytest

from shortforge.providers import store as S
from shortforge.providers import detect as D


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))


def test_credential_holds_many_models_and_flattens():
    store = {"providers": [], "credentials": []}
    cred = S.add_credential(store, name="AgentRouter",
                            base_url="https://agentrouter.org/v1", api_key="ar-secret")
    S.add_model(store, cred["id"], model="claude-opus-4-8", category="llm")
    S.add_model(store, cred["id"], model="whisper-1", category="asr")

    llms = S.models_in(store, "llm")
    assert len(llms) == 1
    p = llms[0]
    # credential creds are projected onto each model row (consumer-shaped dict)
    assert p["base_url"] == "https://agentrouter.org/v1" and p["api_key"] == "ar-secret"
    assert p["model"] == "claude-opus-4-8" and p["category"] == "llm"
    assert "AgentRouter" in p["name"]
    assert S.models_in(store, "asr")[0]["model"] == "whisper-1"


def test_models_in_merges_legacy_and_sorts_by_priority():
    store = {"providers": [], "credentials": []}
    # legacy flat provider (priority 0)
    S.add_provider(store, name="Legacy LLM", category="llm", api_key="k", model="legacy-1")
    cred = S.add_credential(store, name="Gw", base_url="https://x/v1", api_key="k2")
    S.add_model(store, cred["id"], model="new-1", category="llm")   # priority 1

    got = S.models_in(store, "llm")
    assert [p["model"] for p in got] == ["legacy-1", "new-1"]        # both, by priority


def test_disabled_credential_hides_all_its_models():
    store = {"providers": [], "credentials": []}
    cred = S.add_credential(store, name="Gw", base_url="https://x/v1", api_key="k")
    S.add_model(store, cred["id"], model="m1", category="tts")
    S.update_credential(store, cred["id"], enabled=False)
    assert S.models_in(store, "tts", enabled_only=True) == []
    assert len(S.models_in(store, "tts", enabled_only=False)) == 1   # still visible when unfiltered


def test_model_toggle_priority_update_delete():
    store = {"providers": [], "credentials": []}
    cred = S.add_credential(store, name="Gw", base_url="https://x/v1", api_key="k")
    a = S.add_model(store, cred["id"], model="a", category="llm")
    b = S.add_model(store, cred["id"], model="b", category="llm")
    assert [p["model"] for p in S.models_in(store, "llm")] == ["a", "b"]
    S.move_model_priority(store, "llm", b["id"], -1)
    assert [p["model"] for p in S.models_in(store, "llm")] == ["b", "a"]
    S.update_model(store, a["id"], enabled=False)
    assert [p["model"] for p in S.models_in(store, "llm", enabled_only=True)] == ["b"]
    S.delete_model(store, b["id"])
    assert S.get_model(store, b["id"]) is None


def test_add_model_rejects_bad_category_and_missing_credential():
    store = {"providers": [], "credentials": []}
    cred = S.add_credential(store, name="Gw", base_url="https://x/v1", api_key="k")
    with pytest.raises(ValueError):
        S.add_model(store, cred["id"], model="m", category="not-a-category")
    with pytest.raises(ValueError):
        S.add_model(store, "nope", model="m", category="llm")


def test_save_load_roundtrip_preserves_credentials(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(path))
    store = {"providers": [], "credentials": []}
    cred = S.add_credential(store, name="Gw", base_url="https://x/v1", api_key="k")
    S.add_model(store, cred["id"], model="m1", category="vision")
    S.save_store(store)
    loaded = S.load_store()
    assert loaded["credentials"][0]["models"][0]["model"] == "m1"
    assert S.models_in(loaded, "vision")[0]["base_url"] == "https://x/v1"


def test_legacy_only_store_still_flattens():
    store = {"providers": []}                       # no "credentials" key at all
    S.add_provider(store, name="Old", category="asr", api_key="k", model="whisper")
    assert S.models_in(store, "asr")[0]["model"] == "whisper"


def test_fetch_models_guards_empty_and_reads_probe(monkeypatch):
    import json
    assert D.fetch_models("", "") == []                              # no network call
    body = json.dumps({"data": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
    monkeypatch.setattr(D, "_get_raw",
                        lambda url, key, **kw: {"status": 200, "body": body, "error": None, "url": url})
    assert D.fetch_models("https://x/v1", "k") == ["a", "b", "c"]


def test_credential_tts_flows_through_router(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(path))
    store = {"providers": [], "credentials": []}
    cred = S.add_credential(store, name="MyVoice", base_url="https://api.x/v1", api_key="k")
    m = S.add_model(store, cred["id"], model="tts-1", category="tts")
    S.update_model(store, m["id"], capabilities={"ssml": True, "emotion": True,
                                                 "languages": ["en", "de"]}, api_shape="openai")
    S.save_store(store)
    from shortforge.providers.tts import build_tts_router
    router = build_tts_router()
    names = [p.name for p in router.providers]
    assert any("tts-1" in n for n in names)                          # credential model is routed
