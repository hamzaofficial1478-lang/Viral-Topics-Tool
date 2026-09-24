"""STEP 0: LLM detection via the SHARED client — URL joining, model, diagnostics."""

import json

import pytest

import shortforge.llm as L
from shortforge.providers import detect as D


# --- BUG 1: URL joining (shared normalizer used by probe AND production) ----- #

def test_normalize_chat_url_all_cases():
    n = L.normalize_chat_url
    assert n("https://integrate.api.nvidia.com/v1") == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert n("https://integrate.api.nvidia.com/v1/") == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert n("https://integrate.api.nvidia.com") == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert n("https://ai-gateway.vercel.sh/v1") == "https://ai-gateway.vercel.sh/v1/chat/completions"
    # never doubles the version segment
    assert "/v1/v1/" not in n("https://x/v1")


def test_anthropic_url_no_double_v1():
    assert L.anthropic_messages_url("https://api.anthropic.com") == "https://api.anthropic.com/v1/messages"
    assert L.anthropic_messages_url("https://gw/v1") == "https://gw/v1/messages"


# --- BUG 2: model is never defaulted ---------------------------------------- #

def test_openai_chat_raw_requires_model():
    r = L.openai_chat_raw("https://x/v1", "key", "", [{"role": "user", "content": "hi"}])
    assert r["status"] is None
    assert "no model configured" in r["error"]


def test_probe_no_model_fails_clearly():
    r = D.probe_llm("https://integrate.api.nvidia.com/v1", "key", "")
    assert r["reachable"] is False
    assert any("no model configured" in n for n in r["notes"])
    assert "/v1/v1/" not in r["request_excerpt"]     # url still correct, no doubling


# --- diagnostics via the shared client (mocked) ----------------------------- #

def _install_fake(monkeypatch, *, chat_status=200, chat_body=None, image_status=200, err=None):
    chat_body = chat_body if chat_body is not None else json.dumps(
        {"choices": [{"message": {"content": "OK"}}]})

    def fake_raw(base_url, api_key, model, messages, *, max_tokens=512, timeout=60, extra=None, **kw):
        url = L.normalize_chat_url(base_url)
        if not model:
            return {"status": None, "body": "", "error": "no model configured for this provider",
                    "url": url, "model": ""}
        is_img = any(isinstance(m.get("content"), list) for m in messages)
        if err and not is_img:
            return {"status": None, "body": "", "error": err, "url": url, "model": model}
        if is_img:
            body = json.dumps({"choices": [{"message": {"content": "red"}}]}) if image_status == 200 else ""
            return {"status": image_status, "body": body, "error": None, "url": url, "model": model}
        return {"status": chat_status, "body": chat_body, "error": None, "url": url, "model": model}

    monkeypatch.setattr(L, "openai_chat_raw", fake_raw)
    monkeypatch.setattr(D, "_get", lambda *a, **k: None)
    monkeypatch.setattr(D, "_post_json", lambda *a, **k: {"status": None, "body": "", "error": "n/a"})


def test_probe_success_sends_configured_model_and_correct_url(monkeypatch):
    seen = {}

    def fake_raw(base_url, api_key, model, messages, *, max_tokens=512, timeout=60, extra=None, **kw):
        seen["model"] = model
        seen["url"] = L.normalize_chat_url(base_url)
        body = json.dumps({"choices": [{"message": {"content": "OK"}}]})
        return {"status": 200, "body": body, "error": None, "url": seen["url"], "model": model}

    monkeypatch.setattr(L, "openai_chat_raw", fake_raw)
    monkeypatch.setattr(D, "_get", lambda *a, **k: None)
    r = D.probe_llm("https://integrate.api.nvidia.com/v1", "key", "minimaxai/minimax-m3")
    assert seen["model"] == "minimaxai/minimax-m3"          # BUG 2: configured model sent
    assert seen["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"  # BUG 1: no double
    assert r["reachable"] and r["api_shape"] == "openai" and r["multimodal"] is True


def test_probe_401_surfaces_status_and_body(monkeypatch):
    _install_fake(monkeypatch, chat_status=401,
                  chat_body=json.dumps({"error": {"message": "invalid api key"}}))
    r = D.probe_llm("https://x/v1", "badkey", "m")
    assert r["reachable"] is False and r["status_code"] == 401
    assert "invalid api key" in r["response_excerpt"]
    assert any("auth failed" in n for n in r["notes"])


def test_probe_timeout_surfaces_error(monkeypatch):
    _install_fake(monkeypatch, err="socket timed out")
    r = D.probe_llm("https://x/v1", "key", "m")
    assert r["reachable"] is False
    assert any("probe error" in n and "timed out" in n for n in r["notes"])


def test_probe_redacts_key(monkeypatch):
    _install_fake(monkeypatch, chat_status=403,
                  chat_body=json.dumps({"error": "leaked supersecretkey999"}))
    r = D.probe_llm("https://x/v1", "supersecretkey999", "m")
    assert "supersecretkey999" not in r["response_excerpt"]
    assert "supersecretkey999" not in r["request_excerpt"]


def test_detect_llm_has_no_tts_flags(monkeypatch):
    _install_fake(monkeypatch, chat_status=200)
    r = D.detect("llm", "https://x/v1", "key", "m")
    assert "ssml" not in r and "emotion" not in r and "cloning" not in r
    assert r["reachable"] is True


def test_has_choices_openai_and_anthropic():
    assert D._has_choices(json.dumps({"choices": [{"message": {"content": "hi"}}]}))
    assert D._has_choices(json.dumps({"content": [{"type": "text", "text": "hi"}]}))
    assert not D._has_choices("not json")
    assert not D._has_choices(json.dumps({"choices": []}))
