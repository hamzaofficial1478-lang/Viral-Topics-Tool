"""L — Unified provider layer: LLM + TTS + audio-library.

One package, three interfaces, capability-driven selection with priority +
failover and per-provider cost tracking. Everything is built from env
(``.env``); adding a provider means implementing an interface here.
"""

from __future__ import annotations

import time

from .base import (AudioLibraryProvider, Capabilities, CostTracker, LLMProvider,
                   SynthResult, TTSProvider)
from .audio_library import build_audio_library
from .llm import build_llm_provider
from .tts import (EdgeTTSProvider, OpenAICompatibleTTSProvider, ProviderError,
                  TTSRouter, build_tts_router)
from . import detect as detect_mod
from . import store as store_mod

__all__ = [
    "Capabilities", "CostTracker", "SynthResult",
    "LLMProvider", "TTSProvider", "AudioLibraryProvider",
    "TTSRouter", "ProviderError", "EdgeTTSProvider", "OpenAICompatibleTTSProvider",
    "build_llm_provider", "build_tts_router", "build_audio_library",
    "check_providers", "detect_mod", "store_mod",
    "test_tts_provider", "capability_warnings", "dub_language_check", "run_failover",
    "call_model_chat", "call_task_chat",
]


def run_failover(chain: list, attempt):
    """Try ``attempt(model)`` down an ordered chain until one succeeds (item 4).

    Returns (result, model_used, failovers) where ``failovers`` is the list of
    (model, error) that were tried and failed first — so the caller can log/record
    every failover (R4). Raises ShortForgeError with all errors if the whole chain
    fails. This is the shared cascade the vision/LLM/TTS tasks use so a fallback
    provably kicks in on error, never silently.
    """
    from ..utils import ShortForgeError
    failovers = []
    for model in chain:
        try:
            return attempt(model), model, failovers
        except Exception as e:  # noqa: BLE001
            failovers.append((model, e))
    tried = ", ".join((m.get("name") or m.get("model") or "?") for m, _ in failovers) or "(none)"
    raise ShortForgeError(f"all providers failed ({tried}): "
                          + " | ".join(str(e) for _, e in failovers))


def call_model_chat(model: dict, messages: list, *, json_mode: bool = False,
                    max_tokens: int = 800, timeout: int = 60) -> str:
    """One chat-completion against a single flattened store model, via the SHARED
    client (openai_chat_raw). Returns the message content; raises on non-200.

    This is the production LLM path (Forge gpt-luna-5.6, minimax-m3, …) — the
    same URL/request construction as the probe, so they cannot drift (R7)."""
    import json as _json
    from ..llm import openai_chat_raw
    from ..utils import ShortForgeError
    extra = {"response_format": {"type": "json_object"}} if json_mode else None
    r = openai_chat_raw(model.get("base_url", ""), model.get("api_key", ""),
                        model.get("model", ""), messages, max_tokens=max_tokens,
                        timeout=timeout, extra=extra)
    if r["status"] != 200:
        raise ShortForgeError(f"{model.get('name') or model.get('model')}: "
                              f"HTTP {r['status']} {(r['body'] or r['error'] or '')[:200]}")
    try:
        return _json.loads(r["body"])["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        raise ShortForgeError(f"unexpected response from {model.get('model')}: {e}") from None


def call_task_chat(store: dict, task_key: str, messages: list, *, json_mode: bool = False,
                   max_tokens: int = 800):
    """Resolve a task's provider chain and call chat with failover (R1 + R4 seam).

    Returns (content, model_used, failovers). Raises ShortForgeError if the whole
    chain fails. Used by hook scoring / translation / metadata / semantic checks."""
    from .store import resolve_task
    from ..utils import ShortForgeError
    chain = resolve_task(store, task_key)
    if not chain:
        raise ShortForgeError(f"no provider configured for task '{task_key}'")
    return run_failover(chain, lambda m: call_model_chat(
        m, messages, json_mode=json_mode, max_tokens=max_tokens))


def dub_language_check(store: dict, language: str) -> tuple[bool, str]:
    """STEP 3: does an enabled TTS provider support the dub target language?

    Returns (ok, message). ok=False means fail-loud (no configured voice provider
    can speak this language with its configured model). If no TTS providers are
    configured at all, returns ok=True (the offline engine handles it).
    """
    from .store import models_in
    lang = (language or "").split("-")[0].lower()
    tts = models_in(store, "tts", enabled_only=True)
    if not tts or not lang:
        return True, ""
    for p in tts:
        caps = p.get("capabilities") or {}
        langs = caps.get("languages")
        # None/empty = unknown coverage → permissive (can't prove unsupported).
        if not langs or lang in [str(x).split("-")[0].lower() for x in langs]:
            return True, ""
    # None supports it — suggest a model (from model_languages) that does.
    suggestion = ""
    for p in tts:
        ml = (p.get("capabilities") or {}).get("model_languages") or {}
        for mid, mlangs in ml.items():
            if lang in mlangs:
                suggestion = f" Provider '{p['name']}' model '{mid}' supports it — set that model."
                break
        if suggestion:
            break
    names = ", ".join(p.get("name", "?") for p in tts)
    if not suggestion:
        suggestion = (" Use a multilingual model (e.g. eleven_multilingual_v2 on "
                      "ElevenLabs) or add a provider that supports this language.")
    return False, (f"No configured voice provider ({names}) supports dub language "
                   f"'{language}' with its current model.{suggestion}")


def test_tts_provider(cfg: dict, out_path: str) -> dict:
    """Run a real sample synth for a single stored TTS provider config.

    Returns {ok, detail, path?}. Used by the settings UI's per-provider Test.
    """
    import time
    if (cfg.get("name", "").lower() == "edge") or cfg.get("api_shape") == "edge":
        prov = EdgeTTSProvider(cfg.get("name", "edge"))
    else:
        prov = OpenAICompatibleTTSProvider.from_config(cfg)
    if not prov.available():
        return {"ok": False, "detail": "no API key configured"}
    t0 = time.time()
    try:
        prov.synthesize("This is a ShortForge voice test.", language="en", out_path=out_path)
    except Exception as e:  # noqa: BLE001
        from .base import redact
        return {"ok": False, "detail": redact(str(e), cfg.get("api_key"))}
    import os
    return {"ok": True, "path": out_path,
            "detail": f"OK ({(time.time()-t0)*1000:.0f} ms, "
                      f"{os.path.getsize(out_path) if os.path.isfile(out_path) else 0} bytes)"}


def capability_warnings(store: dict) -> list[str]:
    """Warn when no enabled provider in a category supports a needed capability."""
    from .store import models_in
    warns = []
    tts = models_in(store, "tts", enabled_only=True)
    if not any(p.get("capabilities", {}).get("emotion") is True or p.get("name", "").lower() != "edge"
               and p.get("capabilities", {}).get("emotion") for p in tts):
        if not any(p.get("capabilities", {}).get("emotion") is True for p in tts):
            warns.append("No configured voice provider supports emotion/style control — "
                         "dubs (PART H2) will use flatter delivery.")
    if not any(p.get("capabilities", {}).get("ssml") is True for p in tts):
        warns.append("No configured voice provider accepts SSML — prosody transfer (H2) "
                     "will fall back to plain text + native params only.")
    if not any(p.get("capabilities", {}).get("cloning") is True for p in tts):
        warns.append("No configured voice provider supports cloning — dub mode 'clone' (H4) "
                     "is unavailable.")
    if not models_in(store, "llm", enabled_only=True):
        warns.append("No LLM provider configured — hook detection, translation and metadata "
                     "use local heuristics only.")
    return warns


def check_providers(cache_dir: str | None = None) -> str:
    """Test every configured provider; return a report (keys always redacted)."""
    lines = ["ShortForge providers — configuration check", "=" * 60]

    # LLM
    llm = build_llm_provider()
    lines.append("[LLM]")
    lines.append("  " + llm.describe())
    if llm.available():
        ok, detail = llm.check()
        lines.append(f"  test: {detail}")

    # TTS (priority order + capabilities + a sample synth path)
    router = build_tts_router(cache_dir=cache_dir)
    lines.append("[TTS] (priority order)")
    for p in router.providers:
        mark = "✓" if p.available() else "✗ (no key/dep)"
        c = p.caps
        lines.append(f"  {mark} {p.name}: langs={('any' if c.languages is None else ','.join(sorted(c.languages)))} "
                     f"ssml={c.ssml} emotion={c.emotion} clone={c.cloning} "
                     f"max_chars={c.max_chars} cost/1k=${c.cost_per_1k_chars}")
        if p.available():
            import tempfile
            t0 = time.time()
            try:
                out = tempfile.mktemp(suffix=".wav")
                p.synthesize("This is a ShortForge test.", language="en", out_path=out)
                import os
                sz = os.path.getsize(out) if os.path.isfile(out) else 0
                lines.append(f"      sample synth: OK ({(time.time()-t0)*1000:.0f} ms, {sz} bytes)")
            except Exception as e:  # noqa: BLE001
                lines.append(f"      sample synth: FAILED ({e})")

    # Audio library
    lib = build_audio_library()
    lines.append("[Audio library]")
    lines.append("  " + lib.describe() + (" ✓" if lib.available() else " ✗"))

    lines.append("=" * 60)
    return "\n".join(lines)
