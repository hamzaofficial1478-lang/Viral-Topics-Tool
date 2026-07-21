"""R1 — per-task provider binding + R2 — cost-tier guard (free-only enforcement)."""

import pytest

from shortforge.providers import store as S


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))


def _store_with_vision(*, free=False, paid=False):
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="NV", base_url="https://x/v1", api_key="k")
    if free:
        S.add_model(store, cred["id"], model="meta/llama-3.2-11b-vision-instruct",
                    category="vision", tier="free")
    if paid:
        S.add_model(store, cred["id"], model="some/paid-vlm", category="vision", tier="paid")
    return store, cred


def test_all_14_tasks_defined():
    keys = [t["key"] for t in S.TASKS]
    assert len(keys) == 14 and len(set(keys)) == 14
    assert "hook_detection" in keys and "vision_scoring" in keys and "ocr" in keys


def test_free_only_tasks_cannot_be_made_paid():
    store = {"providers": [], "credentials": [], "tasks": {}}
    S.set_task_binding(store, "vision_scoring", paid_allowed=True)   # attempt to enable paid
    assert S.get_task_binding(store, "vision_scoring")["paid_allowed"] is False
    # a normal task can allow paid
    S.set_task_binding(store, "hook_detection", paid_allowed=True)
    assert S.get_task_binding(store, "hook_detection")["paid_allowed"] is True


def test_explicit_binding_orders_the_chain():
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="G", base_url="https://x/v1", api_key="k")
    a = S.add_model(store, cred["id"], model="a", category="llm")
    b = S.add_model(store, cred["id"], model="b", category="llm")
    S.set_task_binding(store, "dub_translation", primary=b["id"], fallback=a["id"])
    chain = S.resolve_task(store, "dub_translation")
    assert [m["model"] for m in chain] == ["b", "a"]


def test_unbound_task_falls_back_to_priority_order():
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="G", base_url="https://x/v1", api_key="k")
    S.add_model(store, cred["id"], model="a", category="llm")
    S.add_model(store, cred["id"], model="b", category="llm")
    chain = S.resolve_task(store, "metadata")          # no explicit binding
    assert [m["model"] for m in chain] == ["a", "b"]   # category priority order


def test_free_only_guard_filters_to_free_and_flags_violation():
    # only a PAID vision model available for a free-only task
    store, _ = _store_with_vision(free=False, paid=True)
    assert S.resolve_task(store, "vision_scoring") == []          # paid refused
    assert "FREE-only" in S.task_paid_violation(store, "vision_scoring")


def test_free_model_satisfies_free_only_task():
    store, _ = _store_with_vision(free=True, paid=True)
    chain = S.resolve_task(store, "vision_scoring")
    assert [m["model"] for m in chain] == ["meta/llama-3.2-11b-vision-instruct"]  # only the free one
    assert S.task_paid_violation(store, "vision_scoring") == ""


def test_no_models_is_not_a_paid_violation():
    store = {"providers": [], "credentials": [], "tasks": {}}
    assert S.task_paid_violation(store, "ocr") == ""             # nothing to spend on yet


def test_disabled_task_resolves_empty():
    store, _ = _store_with_vision(free=True)
    S.set_task_binding(store, "vision_scoring", enabled=False)
    assert S.resolve_task(store, "vision_scoring") == []


def test_model_tier_roundtrips_and_flattens():
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="G", base_url="https://x/v1", api_key="k")
    m = S.add_model(store, cred["id"], model="v", category="vision", tier="free")
    assert m["tier"] == "free"
    assert S.models_in(store, "vision")[0]["tier"] == "free"
    S.update_model(store, m["id"], tier="paid")
    assert S.models_in(store, "vision")[0]["tier"] == "paid"


def test_builtin_local_backends_are_routable_and_free():
    assert any(m["id"] == "builtin:edge-tts" and m["tier"] == "free"
               for m in S.builtin_models("tts"))
    assert any(m["id"] == "builtin:local-whisper" and m["tier"] == "free"
               for m in S.builtin_models("asr"))
    # empty store still resolves the local backends for their tasks
    store = {"providers": [], "credentials": [], "tasks": {}}
    assert [m["model"] for m in S.resolve_task(store, "asr")] == ["small"]        # local Whisper
    assert [m["model"] for m in S.resolve_task(store, "tts_volume")] == ["edge"]  # edge-tts


def test_builtins_are_fallbacks_behind_stored_models():
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="EL", base_url="https://api.elevenlabs.io", api_key="k")
    S.add_model(store, cred["id"], model="eleven_multilingual_v2", category="tts", tier="paid")
    chain = [m["model"] for m in S.resolve_task(store, "tts_quality")]
    assert chain == ["eleven_multilingual_v2", "edge"]        # stored first, edge builtin last


def test_bind_task_to_a_builtin():
    store = {"providers": [], "credentials": [], "tasks": {}}
    S.set_task_binding(store, "tts_volume", primary="builtin:edge-tts")
    assert [m["model"] for m in S.resolve_task(store, "tts_volume")] == ["edge"]


def test_routable_models_includes_builtins():
    store = {"providers": [], "credentials": [], "tasks": {}}
    ids = [m["id"] for m in S.routable_models(store, "tts")]
    assert "builtin:edge-tts" in ids


def test_task_binding_survives_save_load(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(path))
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="G", base_url="https://x/v1", api_key="k")
    m = S.add_model(store, cred["id"], model="a", category="llm")
    S.set_task_binding(store, "hook_detection", primary=m["id"])
    S.save_store(store)
    loaded = S.load_store()
    assert S.get_task_binding(loaded, "hook_detection")["primary"] == m["id"]
