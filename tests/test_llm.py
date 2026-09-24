"""E: configurable LLM provider resolution + redaction + graceful degradation."""

import pytest

from shortforge import llm


def _clear(monkeypatch):
    for k in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_none_when_unset(monkeypatch):
    _clear(monkeypatch)
    c = llm.resolve()
    assert c.provider == "none"
    assert llm.available() is False


def test_backcompat_anthropic_key(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-legacy")
    c = llm.resolve()
    assert c.provider == "anthropic" and c.api_key == "sk-ant-legacy"
    assert llm.available() is True


def test_openai_provider_with_gateway(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://gw.example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "gw-key-123456")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    c = llm.resolve()
    assert c.provider == "openai"
    assert c.base_url == "https://gw.example.com/v1"
    assert c.model == "gpt-4o-mini"


def test_unknown_provider_defaults_to_openai_shape(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "someproxy")
    monkeypatch.setenv("LLM_API_KEY", "k")
    assert llm.resolve().provider == "openai"


def test_explicit_none_overrides_legacy(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    assert llm.resolve().provider == "none"
    assert llm.available() is False


def test_describe_redacts_key(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "supersecretkey123")
    d = llm.describe()
    assert "supersecretkey123" not in d
    assert "sup" in d and "23" in d  # redacted form shows only edges


def test_redact_strips_key_from_text(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "supersecretkey123")
    assert "supersecretkey123" not in llm.redact("error with supersecretkey123 in it")


def test_complete_json_raises_without_provider(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(llm.LLMError):
        llm.complete_json("hi", {"type": "object"})


def test_check_reports_no_provider(monkeypatch):
    _clear(monkeypatch)
    ok, detail = llm.check()
    assert ok is False and "no provider" in detail.lower()
