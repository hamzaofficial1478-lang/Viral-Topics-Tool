"""E — Configurable LLM provider.

One abstraction every LLM call goes through (translation, hook detection,
metadata), so ShortForge is not hardwired to Anthropic. Configured via .env:

    LLM_PROVIDER = anthropic | openai | none
    LLM_BASE_URL = https://your-gateway.example.com/v1   (optional)
    LLM_API_KEY  = ...
    LLM_MODEL    = ...

- anthropic → Anthropic-style client, honouring LLM_BASE_URL if set.
- openai    → OpenAI-compatible /chat/completions (what most third-party
              gateways speak; the safest default for an unknown provider).
- none      → no LLM; callers use their local/heuristic path.

Back-compat: if the LLM_* vars are unset but ANTHROPIC_API_KEY exists, behave
exactly as before (Anthropic, default endpoint). The API key is never logged and
is redacted from any error text.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass

from ..utils import log

_DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


@dataclass
class LLMConfig:
    provider: str            # anthropic | openai | none
    api_key: str
    model: str
    base_url: str | None


class LLMError(RuntimeError):
    pass


def _redact(text: str, key: str | None) -> str:
    if key and len(key) > 6:
        text = text.replace(key, key[:3] + "…" + key[-2:])
    return text


def redact(text: str) -> str:
    """Redact any configured key from arbitrary text (logs/tracebacks)."""
    return _redact(text, resolve().api_key)


def _from_store() -> "LLMConfig | None":
    """Prefer an enabled LLM provider configured through the settings UI."""
    try:
        from ..providers.store import load_store, providers_in
        configured = providers_in(load_store(), "llm", enabled_only=True)
    except Exception:  # noqa: BLE001
        return None
    if not configured:
        return None
    p = configured[0]
    if not p.get("api_key"):
        return None
    shape = p.get("api_shape")
    provider = "anthropic" if shape == "anthropic" else "openai"
    return LLMConfig(provider, p.get("api_key", ""),
                     p.get("model") or (_DEFAULT_ANTHROPIC_MODEL if provider == "anthropic"
                                        else _DEFAULT_OPENAI_MODEL),
                     p.get("base_url") or None)


def resolve() -> LLMConfig:
    """Resolve provider config: settings-UI store first, then environment."""
    from_store = _from_store()
    if from_store is not None:
        return from_store
    provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    base = os.environ.get("LLM_BASE_URL") or None

    if not provider:
        # Back-compat: fall back to the historical Anthropic env vars.
        legacy = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if legacy:
            return LLMConfig("anthropic", legacy, model or _DEFAULT_ANTHROPIC_MODEL,
                             os.environ.get("ANTHROPIC_BASE_URL"))
        return LLMConfig("none", "", "", None)

    if provider == "none":
        return LLMConfig("none", "", "", None)
    if provider == "anthropic":
        return LLMConfig("anthropic", key or os.environ.get("ANTHROPIC_API_KEY", ""),
                         model or _DEFAULT_ANTHROPIC_MODEL, base)
    # default unknown providers to the OpenAI-compatible shape
    return LLMConfig("openai", key, model or _DEFAULT_OPENAI_MODEL, base)


def available() -> bool:
    c = resolve()
    return c.provider != "none" and bool(c.api_key)


def describe() -> str:
    c = resolve()
    if c.provider == "none":
        return "LLM: none (local/heuristic paths only)"
    key = (c.api_key[:3] + "…" + c.api_key[-2:]) if len(c.api_key) > 6 else "(set)" if c.api_key else "(missing)"
    return f"LLM: {c.provider} model={c.model} base={c.base_url or 'default'} key={key}"


# --- unified JSON completion ------------------------------------------------ #

def complete_json(prompt: str, schema: dict, *, system: str | None = None,
                  max_tokens: int = 1200) -> dict:
    """Return a parsed JSON object from the configured provider."""
    c = resolve()
    if c.provider == "none" or not c.api_key:
        raise LLMError("no LLM provider configured")
    try:
        if c.provider == "anthropic":
            text = _anthropic_json(c, prompt, schema, system, max_tokens)
        else:
            text = _openai_json(c, prompt, schema, system, max_tokens)
    except Exception as e:  # noqa: BLE001
        raise LLMError(_redact(str(e), c.api_key)) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"provider returned non-JSON: {text[:200]}") from e


def _anthropic_json(c: LLMConfig, prompt: str, schema: dict,
                    system: str | None, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError:
        return _anthropic_json_http(c, prompt, schema, system, max_tokens)
    kwargs = {"api_key": c.api_key}
    if c.base_url:
        kwargs["base_url"] = c.base_url
    client = anthropic.Anthropic(**kwargs)
    req = {
        "model": c.model, "max_tokens": max_tokens,
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        req["system"] = system
    resp = client.messages.create(**req)
    return next((b.text for b in resp.content if b.type == "text"), "{}")


def _http_post(url: str, headers: dict, payload: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _anthropic_json_http(c: LLMConfig, prompt: str, schema: dict,
                         system: str | None, max_tokens: int) -> str:
    base = (c.base_url or "https://api.anthropic.com").rstrip("/")
    url = base + "/v1/messages"
    headers = {
        "x-api-key": c.api_key, "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": c.model, "max_tokens": max_tokens,
        "messages": [{"role": "user",
                      "content": prompt + "\n\nReturn ONLY valid JSON matching this "
                      f"schema:\n{json.dumps(schema)}"}],
    }
    if system:
        payload["system"] = system
    data = _http_post(url, headers, payload)
    blocks = data.get("content", [])
    return next((b.get("text", "") for b in blocks if b.get("type") == "text"), "{}")


def _openai_json(c: LLMConfig, prompt: str, schema: dict,
                 system: str | None, max_tokens: int) -> str:
    base = (c.base_url or "https://api.openai.com/v1").rstrip("/")
    url = base + "/chat/completions"
    headers = {"Authorization": f"Bearer {c.api_key}", "content-type": "application/json"}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt +
                     "\n\nReturn ONLY valid JSON matching this schema:\n"
                     + json.dumps(schema)})
    payload = {
        "model": c.model, "max_tokens": max_tokens, "messages": messages,
        "response_format": {"type": "json_object"},
    }
    data = _http_post(url, headers, payload)
    return data["choices"][0]["message"]["content"]


def check() -> tuple[bool, str]:
    """Send a tiny prompt; return (ok, human-readable detail). Key redacted."""
    c = resolve()
    if c.provider == "none" or not c.api_key:
        return False, "no provider configured (LLM_PROVIDER/LLM_API_KEY unset)"
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"], "additionalProperties": False}
    t0 = time.time()
    try:
        complete_json('Reply with {"ok": true}.', schema, max_tokens=50)
    except Exception as e:  # noqa: BLE001
        return False, f"{describe()} -> FAILED: {_redact(str(e), c.api_key)}"
    return True, f"{describe()} -> OK ({(time.time() - t0) * 1000:.0f} ms)"
