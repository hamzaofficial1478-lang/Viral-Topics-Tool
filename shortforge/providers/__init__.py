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
    "test_tts_provider", "capability_warnings",
]


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
    from .store import providers_in
    warns = []
    tts = providers_in(store, "tts", enabled_only=True)
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
    if not providers_in(store, "llm", enabled_only=True):
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
