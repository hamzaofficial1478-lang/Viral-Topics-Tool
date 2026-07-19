"""L — TTS providers + router (priority, failover, cost, caching, chunking).

Concrete providers:
- ``OpenAICompatibleTTSProvider`` — POST ``{base}/audio/speech`` (the OpenAI TTS
  shape most gateways speak). Capabilities are declared via env, so an emotion/
  SSML-capable gateway advertises those and the router can prefer it.
- ``EdgeTTSProvider`` — Microsoft Edge neural voices (local, free, no SSML/emotion).

The ``TTSRouter`` tries providers in configured priority order, filtering by the
capabilities a call requires (language, SSML, emotion), fails over on error or
unsupported language, tracks per-provider cost, chunks long input, and caches on
``(text, provider, voice, ssml, style)`` so identical lines are never re-synthesized.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request

from ..utils import ShortForgeError, log, require_binary, run
from .base import Capabilities, CostTracker, SynthResult, TTSProvider, _env, redact


class ProviderError(ShortForgeError):
    pass


# --- concrete providers ----------------------------------------------------- #

class OpenAICompatibleTTSProvider(TTSProvider):
    def __init__(self, name: str, prefix: str):
        self.name = name
        self._prefix = prefix
        self.base_url = _env(f"{prefix}_BASE_URL") or "https://api.openai.com/v1"
        self.api_key = _env(f"{prefix}_API_KEY")
        self.model = _env(f"{prefix}_MODEL") or "tts-1"
        self.caps = Capabilities.from_env(prefix)

    def available(self) -> bool:
        return bool(self.api_key)

    def voice_for(self, language: str) -> str | None:
        lang = (language or "en").split("-")[0].upper()
        return _env(f"{self._prefix}_VOICE_{lang}") or _env(f"{self._prefix}_VOICE") or None

    def synthesize(self, text, *, language, out_path, voice=None, ssml=False, style=None):
        voice = voice or self.voice_for(language)
        url = self.base_url.rstrip("/") + "/audio/speech"
        payload = {"model": self.model, "input": text,
                   "voice": voice or "alloy", "response_format": "wav"}
        if ssml:
            payload["input_format"] = "ssml"       # honoured by SSML-capable gateways
        if style:
            payload.update(style)                   # emotion/style params, vendor-specific
        headers = {"Authorization": f"Bearer {self.api_key}", "content-type": "application/json"}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                audio = resp.read()
        except Exception as e:  # noqa: BLE001
            raise ProviderError(redact(f"{self.name} synth failed: {e}", self.api_key)) from None
        with open(out_path, "wb") as f:
            f.write(audio)
        chars = len(text)
        return SynthResult(out_path, self.name, chars,
                           chars / 1000.0 * self.caps.cost_per_1k_chars,
                           voice=voice, style=json.dumps(style) if style else None)


class EdgeTTSProvider(TTSProvider):
    _VOICES = {"en": "en-US-AriaNeural", "de": "de-DE-KatjaNeural",
               "es": "es-ES-ElviraNeural", "it": "it-IT-ElsaNeural",
               "fr": "fr-FR-DeniseNeural", "ja": "ja-JP-NanamiNeural",
               "pt": "pt-BR-FranciscaNeural", "ar": "ar-SA-ZariyahNeural"}

    def __init__(self, name: str = "edge"):
        self.name = name
        self.caps = Capabilities(languages=set(self._VOICES), ssml=False,
                                 emotion=False, cloning=False, max_chars=6000,
                                 cost_per_1k_chars=0.0)

    def available(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False

    def voice_for(self, language: str) -> str | None:
        return (_env(f"EDGE_VOICE_{(language or 'en').split('-')[0].upper()}")
                or self._VOICES.get((language or "en").split("-")[0], "en-US-AriaNeural"))

    def synthesize(self, text, *, language, out_path, voice=None, ssml=False, style=None):
        import asyncio
        import edge_tts

        voice = voice or self.voice_for(language)
        mp3 = out_path + ".mp3"

        async def _go():
            await edge_tts.Communicate(text, voice).save(mp3)

        asyncio.run(_go())
        run([require_binary("ffmpeg"), "-y", "-i", mp3, "-ar", "48000", "-ac", "2", out_path])
        try:
            os.remove(mp3)
        except OSError:
            pass
        return SynthResult(out_path, self.name, len(text), 0.0, voice=voice)


# --- router ----------------------------------------------------------------- #

class TTSRouter:
    def __init__(self, providers: list[TTSProvider], cache_dir: str | None = None):
        self.providers = providers
        self.cache_dir = cache_dir
        self.cost = CostTracker()

    def describe(self) -> list[str]:
        return [f"{'✓' if p.available() else '✗'} {p.describe()}" for p in self.providers]

    def _cache_path(self, key: str) -> str | None:
        if not self.cache_dir:
            return None
        os.makedirs(self.cache_dir, exist_ok=True)
        return os.path.join(self.cache_dir, key + ".wav")

    def synthesize(self, text: str, *, language: str, out_path: str,
                   need_ssml: bool = False, need_emotion: bool = False,
                   voice: str | None = None, style: dict | None = None) -> SynthResult:
        eligible = [p for p in self.providers if p.available()
                    and p.supports(language=language, ssml=need_ssml, emotion=need_emotion)]
        if not eligible:
            raise ProviderError(
                f"No configured TTS provider supports language={language} "
                f"ssml={need_ssml} emotion={need_emotion}. Configure a capable "
                f"provider in .env (TTS_PROVIDERS + capability flags) or relax the "
                f"requirement.")

        key = hashlib.sha256(
            f"{text}|{language}|{voice}|{need_ssml}|{json.dumps(style, sort_keys=True)}"
            .encode()).hexdigest()[:24]

        last_err = None
        for p in eligible:
            cache_key = f"{p.name}_{key}"
            cp = self._cache_path(cache_key)
            if cp and os.path.isfile(cp) and os.path.getsize(cp) > 0:
                import shutil
                shutil.copyfile(cp, out_path)
                log.info("tts: cache hit (%s)", p.name)
                return SynthResult(out_path, p.name, len(text), 0.0, cached=True, voice=voice)
            try:
                res = self._synth_with_chunking(p, text, language, out_path, voice, need_ssml, style)
                self.cost.add(p.name, res.chars, res.cost)
                if cp:
                    import shutil
                    shutil.copyfile(out_path, cp)
                log.info("tts: %s served %d chars (cost $%.4f)", p.name, res.chars, res.cost)
                return res
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("tts: provider %s failed (%s); failing over", p.name, e)
        raise ProviderError(f"All TTS providers failed. Last error: {last_err}")

    def _synth_with_chunking(self, p: TTSProvider, text, language, out_path, voice, ssml, style):
        if len(text) <= p.caps.max_chars:
            return p.synthesize(text, language=language, out_path=out_path,
                                voice=voice, ssml=ssml, style=style)
        # split on sentence-ish boundaries into <= max_chars chunks, concat
        import re
        parts, cur = [], ""
        for tok in re.split(r"(?<=[.!?])\s+", text):
            if len(cur) + len(tok) + 1 > p.caps.max_chars and cur:
                parts.append(cur)
                cur = tok
            else:
                cur = (cur + " " + tok).strip()
        if cur:
            parts.append(cur)
        tmp = []
        total_cost = 0.0
        for i, part in enumerate(parts):
            pp = f"{out_path}.part{i}.wav"
            r = p.synthesize(part, language=language, out_path=pp, voice=voice, ssml=ssml, style=style)
            tmp.append(pp)
            total_cost += r.cost
        listf = out_path + ".concat.txt"
        with open(listf, "w") as f:
            for pp in tmp:
                f.write(f"file '{os.path.abspath(pp)}'\n")
        run([require_binary("ffmpeg"), "-y", "-f", "concat", "-safe", "0",
             "-i", listf, "-ar", "48000", "-ac", "2", out_path])
        for pp in tmp:
            try:
                os.remove(pp)
            except OSError:
                pass
        os.remove(listf)
        return SynthResult(out_path, p.name, len(text), total_cost, voice=voice)


def build_tts_router(cache_dir: str | None = None) -> TTSRouter:
    """Construct the router from ``TTS_PROVIDERS`` (priority-ordered names)."""
    order = [n.strip() for n in _env("TTS_PROVIDERS", "edge").split(",") if n.strip()]
    providers: list[TTSProvider] = []
    for name in order:
        if name.lower() == "edge":
            providers.append(EdgeTTSProvider("edge"))
        else:
            prefix = "TTS_" + name.upper()
            providers.append(OpenAICompatibleTTSProvider(name, prefix))
    if not providers:
        providers.append(EdgeTTSProvider("edge"))
    return TTSRouter(providers, cache_dir=cache_dir)
