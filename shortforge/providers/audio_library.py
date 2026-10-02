"""L — Audio library providers (used by G2 for music/ambience beds).

- ``LocalAudioLibrary`` (default): the operator's own folder of owned/licensed
  tracks + SFX. Ships empty — you point it at your library. Safest for a
  monetized channel (no Content-ID risk).
- ``ApiAudioLibrary``: a licensed royalty-free / generative-audio API supplied by
  the operator via env. Best-effort OpenAI-style shape; capability of the actual
  endpoint is the operator's responsibility.

Every asset returned carries source + licence so provenance can be recorded in
the manifest.
"""

from __future__ import annotations

import json
import os

from ..utils import log
from .base import AudioLibraryProvider, _env

_AUDIO_EXTS = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac")


class LocalAudioLibrary(AudioLibraryProvider):
    name = "local"

    def __init__(self, path: str | None = None):
        self.path = path or _env("AUDIO_LIB_PATH")

    def available(self) -> bool:
        return bool(self.path) and os.path.isdir(self.path)

    def describe(self) -> str:
        if not self.path:
            return "local: AUDIO_LIB_PATH not set"
        return f"local: {self.path} ({'exists' if os.path.isdir(self.path) else 'missing'})"

    def _index(self) -> list[dict]:
        """Read assets + optional sidecar licences (<name>.json or library.json)."""
        assets: list[dict] = []
        if not self.available():
            return assets
        lib_manifest = os.path.join(self.path, "library.json")
        overrides = {}
        if os.path.isfile(lib_manifest):
            try:
                overrides = {a["file"]: a for a in json.load(open(lib_manifest)).get("assets", [])}
            except Exception:  # noqa: BLE001
                overrides = {}
        for root, _, files in os.walk(self.path):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in _AUDIO_EXTS:
                    meta = overrides.get(fn, {})
                    kind = meta.get("kind") or ("sfx" if "sfx" in root.lower()
                                                else "ambience" if "ambien" in root.lower()
                                                else "music")
                    assets.append({
                        "path": os.path.join(root, fn),
                        "title": meta.get("title", os.path.splitext(fn)[0]),
                        "licence": meta.get("licence", "operator-provided (see library.json)"),
                        "source": "local",
                        "kind": kind,
                        "mood": meta.get("mood"),
                        "energy": meta.get("energy"),
                    })
        return assets

    def find(self, *, mood=None, energy=None, duration=None, kind="music"):
        assets = [a for a in self._index() if a["kind"] == kind]
        if not assets:
            return None
        # Prefer a mood match, then energy proximity, else first.
        if mood:
            m = [a for a in assets if (a.get("mood") or "").lower() == mood.lower()]
            if m:
                assets = m
        if energy is not None:
            assets.sort(key=lambda a: abs((a.get("energy") or 0.5) - energy))
        return assets[0]


class ApiAudioLibrary(AudioLibraryProvider):
    name = "api"

    def __init__(self):
        self.base_url = _env("AUDIO_LIB_BASE_URL")
        self.api_key = _env("AUDIO_LIB_API_KEY")

    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    def describe(self) -> str:
        return f"api: {self.base_url or '(unset)'} key={'set' if self.api_key else 'missing'}"

    def find(self, *, mood=None, energy=None, duration=None, kind="music"):
        # Endpoint shapes vary by vendor; return a descriptor the operator's
        # configured endpoint can fulfil. Kept minimal + best-effort by design.
        if not self.available():
            return None
        log.info("audio api: requesting %s (mood=%s energy=%s)", kind, mood, energy)
        return {
            "url": self.base_url.rstrip("/") + "/search",
            "query": {"mood": mood, "energy": energy, "duration": duration, "kind": kind},
            "title": f"api:{kind}", "licence": "per provider terms", "source": self.base_url,
            "kind": kind,
        }


def build_audio_library() -> AudioLibraryProvider:
    provider = (_env("AUDIO_LIB_PROVIDER", "local")).lower()
    return ApiAudioLibrary() if provider == "api" else LocalAudioLibrary()
