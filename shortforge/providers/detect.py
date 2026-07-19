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
import urllib.request

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

    if category == "llm":
        # Try OpenAI chat shape, then Anthropic messages shape.
        shape, ok = _probe_llm(base_url, api_key, model)
        result["api_shape"] = shape
        result["reachable"] = ok
        result["models"] = _probe_models(base_url, api_key)
        result["notes"].append(f"LLM probe: shape={shape}, reachable={ok}")
        return result

    # vision / audio_library: reachability only
    result["reachable"] = bool(_get(base_url.rstrip("/") + "/models", api_key)
                               or _get(base_url.rstrip("/"), api_key))
    result["notes"].append("capability probing not defined for this category")
    return result


def _probe_llm(base_url: str, api_key: str, model: str) -> tuple[str, bool]:
    base = base_url.rstrip("/")
    payload = json.dumps({"model": model or "gpt-4o-mini",
                          "messages": [{"role": "user", "content": "ping"}],
                          "max_tokens": 5}).encode()
    for ep, shape, hdr in (
        ("/chat/completions", "openai", {"Authorization": f"Bearer {api_key}"}),
        ("/v1/chat/completions", "openai", {"Authorization": f"Bearer {api_key}"}),
    ):
        try:
            req = urllib.request.Request(base + ep, data=payload,
                                         headers={**hdr, "content-type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status < 400:
                    return shape, True
        except Exception:  # noqa: BLE001
            continue
    # Anthropic messages
    try:
        ap = json.dumps({"model": model or "claude-3-5-haiku", "max_tokens": 5,
                         "messages": [{"role": "user", "content": "ping"}]}).encode()
        req = urllib.request.Request(base + "/v1/messages", data=ap,
                                     headers={"x-api-key": api_key,
                                              "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status < 400:
                return "anthropic", True
    except Exception:  # noqa: BLE001
        pass
    return "unknown", False
