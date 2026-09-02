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


def _is_timeout(r: dict) -> bool:
    return r.get("status") is None and "tim" in (r.get("error") or "").lower()


# Transport-level failures worth retrying: the request never got a real HTTP
# answer, so the provider didn't reject us — the socket did. Most common on
# Windows under concurrent load: WinError 10054 "connection forcibly closed".
_RETRYABLE_NET = (
    "10054", "forcibly closed", "reset by peer", "connection reset",
    "connection aborted", "broken pipe", "remotedisconnected",
    "connection refused", "temporarily unavailable", "eof occurred",
    "bad handshake", "connection error",
)


def _is_retryable_net(r: dict) -> bool:
    """A dropped/refused connection (no HTTP status) — retry like a timeout."""
    if r.get("status") is not None:
        return False
    err = (r.get("error") or "").lower()
    return any(s in err for s in _RETRYABLE_NET)


def _is_retryable(r: dict) -> bool:
    """Retry only TRANSPORT failures — the request never reached a server that
    answered (timeout, dropped/refused socket). A real HTTP status means the
    provider *did* answer, so 4xx/5xx cascade to the next model in the chain
    instead: failing over to a healthy provider beats retrying a sick one."""
    return _is_timeout(r) or _is_retryable_net(r)


def call_model_chat(model: dict, messages: list, *, json_mode: bool = False,
                    max_tokens: int = 800, timeout: int = 120, retries: int = 2) -> str:
    """One chat-completion against a single flattened store model, via the SHARED
    client (openai_chat_raw). Returns the message content; raises on non-200.

    Times each attempt (logged at INFO), retries a *timeout* with exponential
    backoff (2s, 4s, …), and reports the configured timeout in the error so a slow
    provider is obvious. The credential's ``timeout`` overrides the argument.
    Same URL/request construction as the probe, so they cannot drift (R7)."""
    import json as _json
    import time
    from ..llm import openai_chat_raw
    from ..utils import ShortForgeError, log
    from .base import redact
    from .store import masked, store_path

    timeout = int(model.get("timeout") or timeout)          # per-credential override
    extra = {"response_format": {"type": "json_object"}} if json_mode else None
    r = None
    for attempt in range(retries + 1):
        t0 = time.time()
        r = openai_chat_raw(model.get("base_url", ""), model.get("api_key", ""),
                            model.get("model", ""), messages, max_tokens=max_tokens,
                            timeout=timeout, extra=extra,
                            auth_style=model.get("auth_style", "bearer"),
                            auth_header_name=model.get("auth_header_name"))
        elapsed = time.time() - t0
        log.info("LLM %s: HTTP %s in %.1fs (attempt %d/%d, timeout %ds)",
                 model.get("model"), r["status"], elapsed, attempt + 1, retries + 1, timeout)
        if r["status"] == 200:
            try:
                return _json.loads(r["body"])["choices"][0]["message"]["content"]
            except Exception as e:  # noqa: BLE001
                raise ShortForgeError(f"unexpected response from {model.get('model')}: {e}") from None
        if _is_retryable(r) and attempt < retries:
            back = 2 ** (attempt + 1)
            why = (f"timed out after {timeout}s" if _is_timeout(r)
                   else f"connection dropped ({(r.get('error') or '')[:80]})")
            log.warning("LLM %s: %s; retry %d/%d in %ds",
                        model.get("model"), why, attempt + 2, retries + 1, back)
            time.sleep(back)
            continue
        break

    where = (f"credential={model.get('credential_id', 'env/legacy')} "
             f"key={masked(model.get('api_key'))} auth={r.get('auth', 'Authorization')} "
             f"model={model.get('model')} url={r.get('url')} store={store_path()}")
    if _is_timeout(r):
        raise ShortForgeError(
            f"{model.get('name') or model.get('model')}: timed out after {timeout}s "
            f"({retries + 1} attempt(s)) — raise the timeout (per-credential 'Request timeout' "
            f"in Settings, or the task default) [{where}]")
    key = model.get("api_key")
    if _is_retryable_net(r):
        # No HTTP status: the socket died, the provider never answered. Saying
        # "HTTP None" reads like a bug — name the real cause and the real fix.
        raise ShortForgeError(
            f"{model.get('name') or model.get('model')}: the connection was dropped by the "
            f"provider after {retries + 1} attempt(s) "
            f"({redact((r.get('error') or '')[:120], key)}). This is "
            f"usually transient network trouble or rate-limiting under concurrent load — lower "
            f"detect.hook_concurrency (try 2) if it repeats. [{where}]")
    body_or_err = redact((r["body"] or r["error"] or "")[:200], key)
    raise ShortForgeError(f"{model.get('name') or model.get('model')}: HTTP "
                          f"{r['status']} [{where}] {body_or_err}")


def call_task_chat(store: dict, task_key: str, messages: list, *, json_mode: bool = False,
                   max_tokens: int = 800, timeout: int | None = None, retries: int = 2):
    """Resolve a task's provider chain and call chat with failover (R1 + R4 seam).

    The per-call timeout defaults to the task's default (store.task_timeout) unless
    overridden here or by the credential. Returns (content, model_used, failovers)."""
    from .store import resolve_task, task_timeout
    from ..utils import ShortForgeError
    chain = resolve_task(store, task_key)
    if not chain:
        raise ShortForgeError(f"no provider configured for task '{task_key}'")
    eff = int(timeout or task_timeout(task_key))
    return run_failover(chain, lambda m: call_model_chat(
        m, messages, json_mode=json_mode, max_tokens=max_tokens, timeout=eff, retries=retries))


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
