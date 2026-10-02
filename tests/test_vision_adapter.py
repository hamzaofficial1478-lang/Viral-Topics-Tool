"""R7 — production adapters: vision image-block requests + task LLM routing.

Both go through the shared openai_chat_raw client (same URL/request construction
as the probe — the /v1/v1 anti-drift rule)."""

import json

import pytest

from shortforge.providers import vision as V
from shortforge.providers import call_model_chat, call_task_chat
from shortforge.providers import store as S
from shortforge.utils import ShortForgeError


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))


# --- vision: image content blocks + batching ------------------------------- #

def test_build_vision_messages_has_text_then_image_blocks():
    msgs = V.build_vision_messages(["data:image/jpeg;base64,AAA", "data:image/jpeg;base64,BBB"],
                                   "score these frames")
    content = msgs[0]["content"]
    assert content[0] == {"type": "text", "text": "score these frames"}
    assert [c["type"] for c in content[1:]] == ["image_url", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_encode_image_is_a_jpeg_data_url(tmp_path):
    f = tmp_path / "f.jpg"
    f.write_bytes(b"\xff\xd8\xff\xe0jpegdata")
    url = V.encode_image(str(f))
    assert url.startswith("data:image/jpeg;base64,") and len(url) > 30


def test_score_frames_batches_by_max_images(tmp_path, monkeypatch):
    # 7 frames, max 3 per request -> 3 batches (3,3,1)
    paths = []
    for i in range(7):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(b"\xff\xd8img")
        paths.append(str(p))
    seen_batches = []

    def fake_chat(base, key, model, messages, **kw):
        imgs = [c for c in messages[0]["content"] if c["type"] == "image_url"]
        seen_batches.append(len(imgs))
        return {"status": 200, "body": json.dumps({"choices": [{"message": {"content": "ok"}}]}),
                "error": None, "url": base, "model": model}

    monkeypatch.setattr("shortforge.providers.vision.openai_chat_raw", fake_chat)
    model = {"base_url": "https://integrate.api.nvidia.com/v1", "api_key": "k",
             "model": "meta/llama-3.2-11b-vision-instruct"}
    results = V.score_frames(model, paths, "score", max_images=3)
    assert seen_batches == [3, 3, 1]
    assert all(r["status"] == 200 for r in results)
    assert [r["images"] for r in results] == [3, 3, 1]


# --- task LLM routing: Forge / minimax through the shared client ------------ #

def _llm_store():
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="Forge AI", base_url="https://www.forge-ai.space/v1",
                            api_key="fk")
    forge = S.add_model(store, cred["id"], model="gpt-5.6-luna", category="llm")
    nv = S.add_credential(store, name="NVIDIA build",
                          base_url="https://integrate.api.nvidia.com/v1", api_key="nv")
    mmx = S.add_model(store, nv["id"], model="minimaxai/minimax-m3", category="llm")
    return store, forge, mmx


def test_call_model_chat_uses_shared_client(monkeypatch):
    captured = {}

    def fake_chat(base, key, model, messages, **kw):
        captured.update(base=base, model=model)
        return {"status": 200, "body": json.dumps({"choices": [{"message": {"content": "hi"}}]}),
                "error": None, "url": base, "model": model}

    monkeypatch.setattr("shortforge.providers.openai_chat_raw", fake_chat, raising=False)
    # patch where call_model_chat imports it
    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake_chat)
    out = call_model_chat({"base_url": "https://www.forge-ai.space/v1", "api_key": "k",
                           "model": "gpt-5.6-luna"}, [{"role": "user", "content": "hi"}])
    assert out == "hi" and captured["model"] == "gpt-5.6-luna"


def test_call_task_chat_cascades_forge_then_minimax(monkeypatch):
    store, forge, mmx = _llm_store()
    S.set_task_binding(store, "dub_translation", primary=forge["id"], fallback=mmx["id"])
    calls = []

    def fake_chat(base, key, model, messages, **kw):
        calls.append(model)
        if model == "gpt-5.6-luna":
            return {"status": 500, "body": "forge down", "error": None, "url": base, "model": model}
        return {"status": 200, "body": json.dumps({"choices": [{"message": {"content": "translated"}}]}),
                "error": None, "url": base, "model": model}

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake_chat)
    content, used, failovers = call_task_chat(store, "dub_translation",
                                              [{"role": "user", "content": "x"}])
    assert content == "translated" and used["model"] == "minimaxai/minimax-m3"
    assert calls == ["gpt-5.6-luna", "minimaxai/minimax-m3"]     # actually cascaded
    assert [m["model"] for m, _ in failovers] == ["gpt-5.6-luna"]


def test_call_task_chat_raises_when_unconfigured():
    store = {"providers": [], "credentials": [], "tasks": {}}
    with pytest.raises(ShortForgeError):
        call_task_chat(store, "dub_translation", [{"role": "user", "content": "x"}])
