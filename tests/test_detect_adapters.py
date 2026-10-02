"""BUG2/3/4 — verbatim model ids, category-based probes, fetch diagnostics."""

import json

import pytest

from shortforge.providers import detect as D
from shortforge.providers import store as S


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))


# --- BUG2: model ids are stored + parsed verbatim (never truncated) --------- #

def test_parse_models_keeps_ids_verbatim():
    body = json.dumps({"data": [
        {"id": "meta/llama-3.2-11b-vision-instruct"},
        {"id": "nvidia/nemotron-ocr-v2"},
        {"id": "minimaxai/minimax-m3"},
    ]})
    assert D._parse_models(body) == [
        "meta/llama-3.2-11b-vision-instruct",
        "nvidia/nemotron-ocr-v2",
        "minimaxai/minimax-m3",
    ]


def test_add_model_stores_exact_id_and_separate_display_name():
    store = {"providers": [], "credentials": []}
    c = S.add_credential(store, name="NV", base_url="https://x/v1", api_key="k")
    m = S.add_model(store, c["id"], model="meta/llama-3.2-11b-vision-instruct",
                    category="vision", display_name="Llama Vision")
    assert m["model"] == "meta/llama-3.2-11b-vision-instruct"     # verbatim, not truncated
    assert m["display_name"] == "Llama Vision"                    # label kept separate
    flat = S.models_in(store, "vision")[0]
    assert flat["model"] == "meta/llama-3.2-11b-vision-instruct"  # sent verbatim downstream


# --- BUG3: probe by CATEGORY with a valid request shape --------------------- #

def test_ocr_probes_with_image_not_plain_chat(monkeypatch):
    captured = {}

    def fake_chat(base, key, model, messages, **kw):
        captured["model"] = model
        captured["messages"] = messages
        return {"status": 200, "body": json.dumps({"choices": [{"message": {"content": "NONE"}}]}),
                "error": None, "url": base + "/chat/completions", "model": model}

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake_chat)
    res = D.detect("ocr", "https://integrate.api.nvidia.com/v1", "k", "nvidia/nemotron-ocr-v2")
    assert res["reachable"] is True
    assert captured["model"] == "nvidia/nemotron-ocr-v2"          # exact id sent
    # the probe carried an image block, not just text
    content = captured["messages"][0]["content"]
    assert any(part.get("type") == "image_url" for part in content)
    assert res["request_excerpt"] and res["response_excerpt"]     # diagnostics present


def test_tts_unknown_vendor_does_real_synth_probe(monkeypatch):
    def fake_probe_tts(base, key, model):
        return {"status": 200, "works": True, "request_excerpt": f"POST {base}/audio/speech",
                "response_excerpt": "(binary audio/wav)"}

    monkeypatch.setattr(D, "_probe_voices", lambda *a, **k: (None, []))
    monkeypatch.setattr(D, "_probe_models", lambda *a, **k: [])
    monkeypatch.setattr(D, "_probe_model_languages", lambda *a, **k: {})
    monkeypatch.setattr(D, "_probe_tts", fake_probe_tts)
    res = D.detect("tts", "https://integrate.api.nvidia.com/v1", "k",
                   "chatterbox-multilingual-tts")
    assert res["reachable"] is True
    assert "TTS synth OK" in " ".join(res["notes"])              # actually probed, not assumed
    assert res["request_excerpt"].endswith("/audio/speech")


def test_ocr_is_a_category():
    assert "ocr" in S.CATEGORIES
    assert "ocr" in S.category_label("ocr").lower() or "text" in S.category_label("ocr").lower()


# --- BUG4: fetch surfaces raw status + body ---------------------------------- #

def test_fetch_models_diag_reports_failure(monkeypatch):
    monkeypatch.setattr(D, "_get_raw",
                        lambda url, key, **kw: {"status": 404, "body": "not found", "error": None,
                                                "url": url})
    diag = D.fetch_models_diag("https://agentrouter.org/v1", "k")
    assert diag["models"] == [] and diag["status"] == 404
    assert diag["url"].endswith("/models") and "not found" in diag["body"]


def test_fetch_models_diag_success(monkeypatch):
    body = json.dumps({"data": [{"id": "a/b-1"}, {"id": "c/d-2"}]})
    monkeypatch.setattr(D, "_get_raw",
                        lambda url, key, **kw: {"status": 200, "body": body, "error": None, "url": url})
    diag = D.fetch_models_diag("https://x/v1", "k")
    assert diag["models"] == ["a/b-1", "c/d-2"] and diag["status"] == 200


def test_fetch_models_guards_empty():
    assert D.fetch_models_diag("", "")["models"] == []
    assert D.fetch_models("", "") == []
