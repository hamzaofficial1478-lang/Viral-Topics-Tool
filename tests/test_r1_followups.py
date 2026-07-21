"""R1 follow-ups folded in: fusion schema, batched flag, legacy migration,
failover cascade, edge-tts registration."""

import pytest

from shortforge.providers import store as S
from shortforge.providers import run_failover
from shortforge.utils import ShortForgeError


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))


# --- item 1: hook detection secondary is a FUSION contributor --------------- #

def test_hook_detection_is_a_fusion_task():
    assert S.is_fusion_task("hook_detection") is True
    assert S.is_fusion_task("dub_translation") is False


def test_resolve_fusion_combines_primary_and_secondary():
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="NV", base_url="https://x/v1", api_key="k")
    llm = S.add_model(store, cred["id"], model="forge/gpt-luna-5.6", category="llm")
    vis = S.add_model(store, cred["id"], model="meta/llama-3.2-11b-vision-instruct",
                      category="vision", tier="free")
    S.set_task_binding(store, "hook_detection", primary=llm["id"], secondary=vis["id"])
    fus = S.resolve_fusion(store, "hook_detection")
    models = [m["model"] for m in fus["contributors"]]
    assert "forge/gpt-luna-5.6" in models and "meta/llama-3.2-11b-vision-instruct" in models


def test_resolve_fusion_defaults_to_one_per_category():
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="NV", base_url="https://x/v1", api_key="k")
    S.add_model(store, cred["id"], model="an-llm", category="llm")
    S.add_model(store, cred["id"], model="a-vlm", category="vision", tier="free")
    fus = S.resolve_fusion(store, "hook_detection")           # no explicit binding
    cats = {m["category"] for m in fus["contributors"]}
    assert cats == {"llm", "vision"}                          # transcript + frames


# --- item 2: emotion labelling is batched ----------------------------------- #

def test_emotion_labelling_is_batched():
    assert S.is_batched_task("emotion_labelling") is True
    assert S.is_batched_task("dub_translation") is False


# --- item 3: legacy migration ----------------------------------------------- #

def test_migrate_legacy_groups_by_credential_and_preserves_bindings():
    store = {"providers": [], "credentials": [], "tasks": {}}
    # two NVIDIA models sharing one URL/key + one ElevenLabs
    a = S.add_provider(store, name="minimax", category="llm",
                       base_url="https://integrate.api.nvidia.com/v1", api_key="nv", model="minimaxai/minimax-m3")
    b = S.add_provider(store, name="glm", category="llm",
                       base_url="https://integrate.api.nvidia.com/v1", api_key="nv", model="z-ai/glm-5.2")
    S.update_provider(store, b["id"], enabled=False)
    S.add_provider(store, name="ElevenLabs", category="tts",
                   base_url="https://api.elevenlabs.io", api_key="el", model="eleven_multilingual_v2")
    S.set_task_binding(store, "dub_translation", primary=a["id"])   # binding by legacy id

    moved = S.migrate_legacy(store)
    assert moved == 3 and store["providers"] == []                  # legacy section gone
    names = {c["name"] for c in store["credentials"]}
    assert "NVIDIA build" in names and "ElevenLabs" in names        # renamed correctly
    nv = next(c for c in store["credentials"] if c["name"] == "NVIDIA build")
    assert len(nv["models"]) == 2                                   # both NVIDIA models grouped
    # id preserved -> the task binding still resolves
    assert [m["model"] for m in S.resolve_task(store, "dub_translation")][0] == "minimaxai/minimax-m3"
    # disabled state preserved
    assert any(m["model"] == "z-ai/glm-5.2" and not m["enabled"] for m in nv["models"])


def test_migrate_legacy_is_idempotent():
    store = {"providers": [], "credentials": [], "tasks": {}}
    S.add_provider(store, name="x", category="llm", base_url="https://x/v1", api_key="k", model="m")
    assert S.migrate_legacy(store) == 1
    assert S.migrate_legacy(store) == 0                             # nothing left to move


# --- item 4: failover cascades on error ------------------------------------- #

def test_run_failover_cascades_then_succeeds():
    chain = [{"name": "llama"}, {"name": "nemotron"}]
    calls = []

    def attempt(m):
        calls.append(m["name"])
        if m["name"] == "llama":
            raise RuntimeError("llama down")
        return f"scored by {m['name']}"

    result, used, failovers = run_failover(chain, attempt)
    assert result == "scored by nemotron" and used["name"] == "nemotron"
    assert calls == ["llama", "nemotron"]                          # actually cascaded
    assert [m["name"] for m, _ in failovers] == ["llama"]


def test_run_failover_raises_when_all_fail():
    with pytest.raises(ShortForgeError):
        run_failover([{"name": "a"}], lambda m: (_ for _ in ()).throw(RuntimeError("boom")))


# --- item 5: edge-tts is registered ----------------------------------------- #

def test_edge_tts_always_registered_as_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "p.json"))
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="EL", base_url="https://api.elevenlabs.io", api_key="k")
    m = S.add_model(store, cred["id"], model="eleven_multilingual_v2", category="tts", tier="paid")
    S.update_model(store, m["id"], api_shape="openai")
    S.save_store(store)
    from shortforge.providers.tts import build_tts_router
    names = [p.name.lower() for p in build_tts_router().providers]
    assert "edge" in names                                         # never ElevenLabs-only
