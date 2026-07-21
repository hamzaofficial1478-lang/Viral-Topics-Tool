"""Forge 401 fix — ONE shared Bearer-header builder (probe = CLI = UI); the key
is stripped so whitespace/newline can't 401 one path but not another; and a 401
names the credential + store + redacted key for instant diagnosis."""

import json

import pytest

from shortforge.llm import bearer_header, openai_chat_raw
from shortforge.providers import call_model_chat
from shortforge.utils import ShortForgeError


def test_bearer_header_strips_whitespace_and_newline():
    assert bearer_header("  abc\n") == {"Authorization": "Bearer abc"}
    assert bearer_header("key") == {"Authorization": "Bearer key"}
    assert bearer_header(None) == {"Authorization": "Bearer "}


def test_openai_chat_raw_sends_stripped_key(monkeypatch):
    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        captured["auth"] = req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    openai_chat_raw("https://www.forge-ai.space/v1", "  forge-key\n", "gpt-5.6-luna",
                    [{"role": "user", "content": "hi"}])
    assert captured["auth"] == "Bearer forge-key"      # no trailing newline/space -> no spurious 401


def test_401_diagnostic_names_credential_store_and_redacted_key(monkeypatch, tmp_path):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "p.json"))

    def fake_chat(base, key, model, messages, **kw):
        return {"status": 401, "body": '{"error":{"message":"Invalid or inactive API key."}}',
                "error": None, "url": base + "/chat/completions", "model": model}

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake_chat)
    with pytest.raises(ShortForgeError) as ei:
        call_model_chat({"base_url": "https://www.forge-ai.space/v1",
                         "api_key": "forge-secret-1234", "model": "gpt-5.6-luna",
                         "credential_id": "abc123", "name": "Forge AI · gpt-5.6-luna"},
                        [{"role": "user", "content": "x"}])
    msg = str(ei.value)
    assert "401" in msg
    assert "credential=abc123" in msg and "••••1234" in msg          # which credential + which key
    assert "store=" in msg and "gpt-5.6-luna" in msg
    assert "forge-secret-1234" not in msg                            # never leak the full key
