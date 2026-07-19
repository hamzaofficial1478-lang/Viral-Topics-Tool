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
from .tts import ProviderError, TTSRouter, build_tts_router

__all__ = [
    "Capabilities", "CostTracker", "SynthResult",
    "LLMProvider", "TTSProvider", "AudioLibraryProvider",
    "TTSRouter", "ProviderError",
    "build_llm_provider", "build_tts_router", "build_audio_library",
    "check_providers",
]


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
