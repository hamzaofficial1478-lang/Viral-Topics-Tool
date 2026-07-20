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
    if isinstance(data, list):
        return [str(m.get("model_id") or m.get("id") or m.get("name"))
                for m in data if isinstance(m, dict)][:200]
    return []


def fetch_models(base_url: str, api_key: str) -> list[str]:
    """R6 — public: model ids exposed by ``GET /v1/models`` (for the UI picker)."""
    if not base_url or not api_key:
        return []
    return _probe_models(base_url, api_key)


def _probe_model_languages(base_url: str, api_key: str) -> dict:
    """Return {model_id: [lang_codes]} from a /models endpoint (STEP 3).

    ElevenLabs' /v1/models returns each model's ``languages`` list, so we can
    report exactly which languages a chosen model supports.
    """
    base = base_url.rstrip("/")
    data = _get(base + "/v1/models", api_key) or _get(base + "/models", api_key)
    items = data if isinstance(data, list) else (
        (data.get("data") or data.get("models") or []) if isinstance(data, dict) else [])
    out: dict[str, list[str]] = {}
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = m.get("model_id") or m.get("id") or m.get("name")
        codes = []
        for l in (m.get("languages") or []):
            if isinstance(l, dict):
                c = l.get("language_id") or l.get("code") or l.get("id")
                if c:
                    codes.append(str(c).split("-")[0].lower())
            elif isinstance(l, str):
                codes.append(l.split("-")[0].lower())
        if mid and codes:
            out[str(mid)] = sorted(set(codes))
    return out


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
        # STEP 3: language coverage is a MODEL property (e.g. ElevenLabs
        # multilingual). Read per-model languages and make the configured model's
        # list authoritative, so coverage is accurate and validatable.
        model_langs = _probe_model_languages(base_url, api_key)
        if model_langs:
            result["reachable"] = True
            result["model_languages"] = model_langs
            if model and model in model_langs:
                result["languages"] = model_langs[model]
                result["notes"].append(
                    f"model '{model}' supports {len(model_langs[model])} language(s): "
                    f"{', '.join(model_langs[model][:12])}")
            elif model:
                result["notes"].append(
                    f"configured model '{model}' not found in the models list; "
                    f"languages inferred from voices only")
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

    if category == "asr":
        # STEP 3.5b: does this endpoint expose a transcription model (Canary /
        # Whisper / Parakeet)? Probe the models list and flag ASR-capable ones.
        res = {"api_shape": "openai", "reachable": False, "asr_models": [],
               "languages": [], "models": [], "notes": []}
        models = _probe_models(base_url, api_key)
        res["models"] = models
        kw = ("canary", "whisper", "parakeet", "riva", "nemo", "asr",
              "transcri", "stt", "speech-to-text")
        asr_models = [m for m in models if any(k in (m or "").lower() for k in kw)]
        res["asr_models"] = asr_models
        if models:
            res["reachable"] = True
        if asr_models:
            res["notes"].append(f"ASR-capable model(s): {', '.join(asr_models[:8])}")
            if any("canary" in (m or "").lower() for m in asr_models):
                res["notes"].append("Canary is multilingual and does speech translation — "
                                    "a good CPU-offload option for French.")
            res["notes"].append("Use the model's exact id above, then Test on real audio "
                                "(POST /audio/transcriptions).")
        else:
            res["notes"].append("No obvious ASR model in /models. The endpoint may still "
                                "accept POST /audio/transcriptions — set the model id and Test "
                                "on real audio to confirm. NVIDIA Canary may live on a different "
                                "endpoint (a NIM/gRPC service) than the chat one.")
        return res

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


def probe_llm(base_url: str, api_key: str, model: str) -> dict:
    """Real minimal chat completion probe with full diagnostics.

    Uses the SAME shared client (``llm.openai_chat_raw``) as production, so URL
    and model handling can never drift. Returns: reachable, model_responds,
    api_shape, multimodal, context_length, status_code, request/response
    excerpts (redacted), notes.
    """
    from ..llm import openai_chat_raw, anthropic_messages_url, normalize_chat_url

    res = {"api_shape": "unknown", "reachable": False, "model_responds": False,
           "multimodal": "unknown", "context_length": None, "status_code": None,
           "request_excerpt": "", "response_excerpt": "", "notes": []}
    if not base_url or not api_key:
        res["notes"].append("base URL and API key required to probe")
        return res
    if not model:
        # No silent default — a wrong/absent model is a common 404 cause.
        res["request_excerpt"] = f"POST {normalize_chat_url(base_url)}\n(no model set)"
        res["notes"].append("no model configured for this provider — set the model ID "
                            "(e.g. minimaxai/minimax-m3) before testing")
        return res

    msgs = [{"role": "user", "content": "Reply with the single word OK."}]
    r = openai_chat_raw(base_url, api_key, model, msgs, max_tokens=8)
    res["request_excerpt"] = f"POST {r['url']}\n" + json.dumps(
        {"model": model, "max_tokens": 8, "messages": msgs})
    res["status_code"] = r["status"]
    res["response_excerpt"] = redact(r["body"] or r["error"] or "", api_key)[:1000]

    if r["status"] == 200 and _has_choices(r["body"]):
        res.update({"api_shape": "openai", "reachable": True, "model_responds": True})
        res["multimodal"] = _probe_multimodal(base_url, api_key, model)
        res["context_length"] = _context_from_models(base_url, api_key, model)
        res["notes"].append(f"chat completion OK (200); multimodal={res['multimodal']}")
        return res

    # Anthropic messages shape as a fallback (version-aware URL, shared helper).
    a_payload = {"model": model, "max_tokens": 8, "messages": msgs}
    ar = _post_json(anthropic_messages_url(base_url),
                    {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                    a_payload, api_key)
    if ar["status"] == 200 and _has_choices(ar["body"]):
        res.update({"api_shape": "anthropic", "reachable": True, "model_responds": True,
                    "status_code": 200})
        res["response_excerpt"] = ar["body"][:1000]
        res["notes"].append("anthropic messages OK (200)")
        return res

    # Not reachable — surface the real reason.
    if res["status_code"] in (401, 403):
        res["notes"].append(f"auth failed (HTTP {res['status_code']}) — check the API key")
    elif r["error"]:
        res["notes"].append(f"probe error: {r['error']} (reasoning models can be slow — "
                            f"the probe waits up to 60s)")
    elif res["status_code"] == 404:
        res["notes"].append(f"HTTP 404 at {r['url']} — check the base URL and that the "
                            f"model '{model}' exists on this provider")
    elif res["status_code"]:
        res["notes"].append(f"HTTP {res['status_code']} — see raw response")
    else:
        res["notes"].append("no response — check base URL / network")
    return res


def _probe_multimodal(base_url: str, api_key: str, model: str) -> bool:
    from ..llm import openai_chat_raw
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "Reply with one word."},
        {"type": "image_url", "image_url": {"url": _TEST_IMAGE}}]}]
    r = openai_chat_raw(base_url, api_key, model, msgs, max_tokens=8)
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
