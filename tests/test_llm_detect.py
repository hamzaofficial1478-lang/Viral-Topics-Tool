"""STEP 0: LLM provider detection — real completion probe + diagnostics."""

import json

from shortforge.providers import detect as D


def _install_fake(monkeypatch, *, chat_status=200, chat_body=None, image_status=200,
                  err=None):
    chat_body = chat_body if chat_body is not None else json.dumps(
        {"choices": [{"message": {"content": "OK"}}]})

    def fake_post(url, headers, payload, api_key, timeout=60):
        is_img = any(isinstance(m.get("content"), list) for m in payload.get("messages", []))
        if err and not is_img:
            return {"status": None, "body": "", "error": err}
        if is_img:
            body = json.dumps({"choices": [{"message": {"content": "red"}}]}) if image_status == 200 else ""
            return {"status": image_status, "body": D.redact(body, api_key), "error": None}
        return {"status": chat_status, "body": D.redact(chat_body, api_key), "error": None}

    monkeypatch.setattr(D, "_post_json", fake_post)
    monkeypatch.setattr(D, "_get", lambda *a, **k: None)


def test_probe_success_openai_and_multimodal(monkeypatch):
    _install_fake(monkeypatch, chat_status=200, image_status=200)
    r = D.probe_llm("https://integrate.api.nvidia.com/v1", "key123", "minimaxai/minimax-m3")
    assert r["reachable"] is True and r["model_responds"] is True
    assert r["api_shape"] == "openai"
    assert r["multimodal"] is True
    assert r["status_code"] == 200


def test_probe_reachable_but_not_multimodal(monkeypatch):
    _install_fake(monkeypatch, chat_status=200, image_status=400)
    r = D.probe_llm("https://integrate.api.nvidia.com/v1", "key123", "z-ai/glm-5.2")
    assert r["reachable"] is True
    assert r["multimodal"] is False


def test_probe_401_surfaces_status_and_body(monkeypatch):
    body = json.dumps({"error": {"message": "invalid api key"}})
    _install_fake(monkeypatch, chat_status=401, chat_body=body)
    r = D.probe_llm("https://integrate.api.nvidia.com/v1", "badkey", "m")
    assert r["reachable"] is False
    assert r["status_code"] == 401
    assert "invalid api key" in r["response_excerpt"]
    assert any("auth failed" in n for n in r["notes"])


def test_probe_timeout_surfaces_error(monkeypatch):
    _install_fake(monkeypatch, err="network: timed out")
    r = D.probe_llm("https://integrate.api.nvidia.com/v1", "key123", "m")
    assert r["reachable"] is False
    assert any("probe error" in n and "timed out" in n for n in r["notes"])


def test_probe_redacts_key_in_excerpts(monkeypatch):
    body = json.dumps({"error": "leaked supersecretkey999 here"})
    _install_fake(monkeypatch, chat_status=403, chat_body=body.replace(
        "supersecretkey999", "supersecretkey999"))
    r = D.probe_llm("https://x/v1", "supersecretkey999", "m")
    assert "supersecretkey999" not in r["response_excerpt"]
    assert "supersecretkey999" not in r["request_excerpt"]


def test_detect_llm_category_has_no_tts_flags(monkeypatch):
    _install_fake(monkeypatch, chat_status=200)
    r = D.detect("llm", "https://x/v1", "key123", "m")
    assert "ssml" not in r and "emotion" not in r and "cloning" not in r
    assert r["reachable"] is True


def test_chat_url_no_double_v1():
    primary, fallback = D._chat_url("https://integrate.api.nvidia.com/v1")
    assert primary == "https://integrate.api.nvidia.com/v1/chat/completions"
    # base without /v1 gets one added
    p2, _ = D._chat_url("https://api.example.com")
    assert p2 == "https://api.example.com/v1/chat/completions"


def test_has_choices_openai_and_anthropic():
    assert D._has_choices(json.dumps({"choices": [{"message": {"content": "hi"}}]}))
    assert D._has_choices(json.dumps({"content": [{"type": "text", "text": "hi"}]}))
    assert not D._has_choices("not json")
    assert not D._has_choices(json.dumps({"choices": []}))
