"""L — Capability auto-detection.

"Test & Detect" probes a provider and works out — without asking the operator
technical questions — its API shape, whether it takes SSML, whether it exposes
emotion/style parameters (and their field names), whether it can clone voices,
and which languages/voices it offers. Detection combines live endpoint probing
with a small table of known-vendor signatures. Anything undetectable is marked
``unknown`` and the caller degrades safely rather than blocking.

Never raises — every probe is best-effort and returns a structured result.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import redact

# 1x1 PNG (red dot) as a data URL — used to probe multimodal/vision support.
_TEST_IMAGE = ("data:image/png;base64,"
               "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGP4z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

# Known-vendor signatures keyed on a substring of the base URL or provider name.
# emotion_params lists the exact request fields that carry style/emotion.
_VENDORS = {
    "elevenlabs": {"shape": "elevenlabs", "ssml": False, "emotion": True,
                   "cloning": True, "emotion_params": ["stability", "similarity_boost", "style"],
                   "voices_ep": "/v1/voices"},
    "openai": {"shape": "openai", "ssml": False, "emotion": True,
               "cloning": False, "emotion_params": ["instructions"], "voices_ep": None},
    "azure": {"shape": "azure", "ssml": True, "emotion": True, "cloning": True,
              "emotion_params": ["style", "styledegree", "role"], "voices_ep": "/voices/list"},
    "microsoft": {"shape": "azure", "ssml": True, "emotion": True, "cloning": True,
                  "emotion_params": ["style", "styledegree"], "voices_ep": "/voices/list"},
    "cartesia": {"shape": "openai", "ssml": False, "emotion": True, "cloning": True,
                 "emotion_params": ["emotion", "speed"], "voices_ep": "/voices"},
    "playht": {"shape": "custom", "ssml": False, "emotion": True, "cloning": True,
               "emotion_params": ["emotion", "voice_engine"], "voices_ep": "/api/v2/voices"},
    "google": {"shape": "google", "ssml": True, "emotion": False, "cloning": False,
               "emotion_params": [], "voices_ep": "/v1/voices"},
    "deepgram": {"shape": "openai", "ssml": False, "emotion": False, "cloning": False,
                 "emotion_params": [], "voices_ep": None},
}


def _match_vendor(base_url: str, name: str) -> dict | None:
    hay = f"{base_url} {name}".lower()
    for key, sig in _VENDORS.items():
        if key in hay:
            return {"vendor": key, **sig}
    return None


def _get(url: str, api_key: str, timeout: int = 15):
    """GET JSON trying a few common auth header styles. None on any failure."""
    for hdr in ({"Authorization": f"Bearer {api_key}"}, {"xi-api-key": api_key},
                {"x-api-key": api_key}):
        try:
            req = urllib.request.Request(url, headers=hdr, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            continue
    return None


def _probe_voices(base_url: str, api_key: str, voices_ep: str | None):
    base = base_url.rstrip("/")
    eps = [voices_ep] if voices_ep else []
    eps += ["/voices", "/v1/voices", "/audio/voices", "/v1/audio/voices"]
    for ep in eps:
        if not ep:
            continue
        data = _get(base + ep, api_key)
        if data is None:
            continue
        items = data.get("voices") if isinstance(data, dict) else data
        if isinstance(items, list) and items:
            voices, langs = [], set()
            for v in items:
                if not isinstance(v, dict):
                    continue
                vid = v.get("voice_id") or v.get("id") or v.get("name") or v.get("ShortName")
                if vid:
                    voices.append(str(vid))
                lang = (v.get("language") or v.get("Locale") or v.get("locale")
                        or (v.get("labels", {}) or {}).get("language"))
                if lang:
                    langs.add(str(lang).split("-")[0].lower())
            if voices:
                return voices[:200], sorted(langs)
    return None, []


def _probe_models(base_url: str, api_key: str):
    data = _get(base_url.rstrip("/") + "/models", api_key) or \
        _get(base_url.rstrip("/") + "/v1/models", api_key)
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
        return [str(m.get("id") or m.get("name")) for m in items if isinstance(m, dict)][:200]
    return []


def detect(category: str, base_url: str, api_key: str, model: str = "") -> dict:
    """Best-effort capability detection. Returns a saved-config-ready dict."""
    result = {
        "api_shape": "unknown", "ssml": "unknown", "emotion": "unknown",
        "cloning": "unknown", "emotion_params": [], "languages": [], "voices": [],
        "models": [], "notes": [], "reachable": False,
    }
    if not base_url or not api_key:
        result["notes"].append("base URL and API key required to detect")
        return result

    sig = _match_vendor(base_url, model or "") or _match_vendor(base_url, "")
    if category in ("tts",):
        voices_ep = sig.get("voices_ep") if sig else None
        voices, langs = _probe_voices(base_url, api_key, voices_ep)
        if voices:
            result["reachable"] = True
            result["voices"] = voices
            result["languages"] = langs
        models = _probe_models(base_url, api_key)
        if models:
            result["reachable"] = True
            result["models"] = models
        if sig:
            result.update({
                "api_shape": sig["shape"], "ssml": sig["ssml"],
                "emotion": sig["emotion"], "cloning": sig["cloning"],
                "emotion_params": sig["emotion_params"],
            })
            result["notes"].append(f"recognized vendor '{sig['vendor']}' — capabilities from signature")
        else:
            result["notes"].append("unknown vendor — assuming OpenAI-compatible /audio/speech; "
                                   "SSML/emotion/cloning left 'unknown' (safe defaults off)")
            result["api_shape"] = "openai"
        return result

    if category in ("llm", "vision"):
        # Real chat-completion probe with full diagnostics (no SSML/emotion flags —
        # those are TTS concepts and only confuse for LLM providers).
        return probe_llm(base_url, api_key, model)

    # vision / audio_library: reachability only
    result["reachable"] = bool(_get(base_url.rstrip("/") + "/models", api_key)
                               or _get(base_url.rstrip("/"), api_key))
    result["notes"].append("capability probing not defined for this category")
    return result


def _post_json(url: str, headers: dict, payload: dict, api_key: str,
               timeout: int = 60) -> dict:
    """POST JSON and ALWAYS return {status, body, error} — never raise.

    Captures the real HTTP status and response body even on error so the UI can
    show why a probe failed ("unknown/False" is undiagnosable). Key redacted.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={**headers, "content-type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return {"status": getattr(resp, "status", 200), "body": redact(body, api_key), "error": None}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return {"status": e.code, "body": redact(body, api_key), "error": None}
    except urllib.error.URLError as e:
        return {"status": None, "body": "", "error": f"network: {e.reason}"}
    except Exception as e:  # noqa: BLE001 (e.g. socket.timeout)
        return {"status": None, "body": "", "error": redact(str(e) or type(e).__name__, api_key)}


def _has_choices(body: str) -> bool:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return False
    ch = data.get("choices") if isinstance(data, dict) else None
    if isinstance(ch, list) and ch:
        return True
    # anthropic shape
    return isinstance(data, dict) and isinstance(data.get("content"), list)


def _chat_url(base: str) -> tuple[str, str]:
    """Return (primary, fallback) chat-completions URLs for a base."""
    base = base.rstrip("/")
    if base.endswith("/v1"):
        return base + "/chat/completions", base + "/v1/chat/completions"
    return base + "/v1/chat/completions", base + "/chat/completions"


def probe_llm(base_url: str, api_key: str, model: str) -> dict:
    """Real minimal chat completion probe with full diagnostics.

    Returns: reachable, model_responds, api_shape, multimodal, context_length,
    status_code, request/response excerpts (redacted), notes.
    """
    res = {"api_shape": "unknown", "reachable": False, "model_responds": False,
           "multimodal": "unknown", "context_length": None, "status_code": None,
           "request_excerpt": "", "response_excerpt": "", "notes": []}
    if not base_url or not api_key:
        res["notes"].append("base URL and API key required to probe")
        return res

    # OpenAI-compatible chat completion (real short prompt, generous timeout for
    # reasoning models that burn internal tokens before responding).
    payload = {"model": model or "gpt-4o-mini", "max_tokens": 8, "stream": False,
               "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
    primary, fallback = _chat_url(base_url)
    res["request_excerpt"] = f"POST {primary}\n{json.dumps(payload)}"
    r = _post_json(primary, {"Authorization": f"Bearer {api_key}"}, payload, api_key)
    if r["status"] in (404, 405):
        r = _post_json(fallback, {"Authorization": f"Bearer {api_key}"}, payload, api_key)
        res["request_excerpt"] = f"POST {fallback}\n{json.dumps(payload)}"
    res["status_code"] = r["status"]
    res["response_excerpt"] = (r["body"] or r["error"] or "")[:1000]

    if r["status"] == 200 and _has_choices(r["body"]):
        res.update({"api_shape": "openai", "reachable": True, "model_responds": True})
        res["multimodal"] = _probe_multimodal(base_url, api_key, model)
        res["context_length"] = _context_from_models(base_url, api_key, model)
        res["notes"].append(f"chat completion OK (200); multimodal={res['multimodal']}")
        return res

    # Try Anthropic messages shape before giving up.
    a_payload = {"model": model or "claude-3-5-haiku-latest", "max_tokens": 8,
                 "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
    a_url = base_url.rstrip("/") + ("/messages" if base_url.rstrip("/").endswith("/v1")
                                    else "/v1/messages")
    ar = _post_json(a_url, {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                    a_payload, api_key)
    if ar["status"] == 200 and _has_choices(ar["body"]):
        res.update({"api_shape": "anthropic", "reachable": True, "model_responds": True})
        res["status_code"] = 200
        res["response_excerpt"] = ar["body"][:1000]
        res["notes"].append("anthropic messages OK (200)")
        return res

    # Not reachable — surface the real reason.
    if res["status_code"] == 401 or res["status_code"] == 403:
        res["notes"].append(f"auth failed (HTTP {res['status_code']}) — check the API key")
    elif r["error"]:
        res["notes"].append(f"probe error: {r['error']} (reasoning models can be slow — "
                            f"the probe waits up to 60s)")
    elif res["status_code"]:
        res["notes"].append(f"HTTP {res['status_code']} — see raw response")
    else:
        res["notes"].append("no response — check base URL / network")
    return res


def _probe_multimodal(base_url: str, api_key: str, model: str) -> bool:
    payload = {"model": model or "gpt-4o-mini", "max_tokens": 8,
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": "Reply with one word."},
                   {"type": "image_url", "image_url": {"url": _TEST_IMAGE}}]}]}
    primary, fallback = _chat_url(base_url)
    r = _post_json(primary, {"Authorization": f"Bearer {api_key}"}, payload, api_key)
    if r["status"] in (404, 405):
        r = _post_json(fallback, {"Authorization": f"Bearer {api_key}"}, payload, api_key)
    return bool(r["status"] == 200 and _has_choices(r["body"]))


def _context_from_models(base_url: str, api_key: str, model: str) -> int | None:
    data = _get(base_url.rstrip("/") + "/models", api_key)
    if isinstance(data, dict):
        for m in (data.get("data") or data.get("models") or []):
            if isinstance(m, dict) and (m.get("id") == model or m.get("name") == model):
                for k in ("context_length", "context_window", "max_context_length"):
                    if isinstance(m.get(k), int):
                        return m[k]
    return None
