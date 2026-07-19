"""L/K — Provider config store (what the settings UI writes; gitignored).

Providers configured through the UI are persisted here as JSON, including their
detected capabilities and priority. The provider layer reads this store first
and falls back to environment variables when it's empty (headless / CI). Keys
live only in this gitignored file — never printed in full, never logged.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from ..utils import log

CATEGORIES = ("tts", "llm", "vision", "audio_library")
_CATEGORY_LABELS = {
    "tts": "Voice / TTS", "llm": "LLM (text & analysis)",
    "vision": "Vision / Analysis", "audio_library": "Audio library (music & SFX)",
}


def store_path() -> str:
    return os.environ.get("SHORTFORGE_PROVIDERS_FILE") or os.path.join(
        "config", "providers.local.json")


def load_store() -> dict:
    path = store_path()
    if not os.path.isfile(path):
        return {"providers": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("providers", [])
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("providers store unreadable (%s); starting empty", e)
        return {"providers": []}


def save_store(store: dict) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    # Keys live here — make sure it never becomes world-readable on POSIX.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def masked(key: str | None) -> str:
    if not key:
        return "(none)"
    return "••••" + key[-4:] if len(key) > 4 else "••••"


def add_provider(store: dict, *, name: str, category: str, base_url: str = "",
                 api_key: str = "", model: str = "", voice: str = "") -> dict:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category '{category}'")
    prov = {
        "id": uuid.uuid4().hex[:8],
        "name": name.strip() or "provider",
        "category": category,
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "model": model.strip(),
        "voice": voice.strip(),
        "enabled": True,
        "priority": len([p for p in store["providers"] if p["category"] == category]),
        "capabilities": {},
        "api_shape": "unknown",
        "voices": [],
        "models": [],
        "last_test": None,
    }
    store["providers"].append(prov)
    return prov


def get_provider(store: dict, pid: str) -> dict | None:
    return next((p for p in store["providers"] if p["id"] == pid), None)


def update_provider(store: dict, pid: str, **fields) -> dict | None:
    p = get_provider(store, pid)
    if p is None:
        return None
    p.update({k: v for k, v in fields.items() if v is not None})
    return p


def delete_provider(store: dict, pid: str) -> None:
    store["providers"] = [p for p in store["providers"] if p["id"] != pid]


def providers_in(store: dict, category: str, *, enabled_only: bool = True) -> list[dict]:
    ps = [p for p in store["providers"] if p["category"] == category
          and (p.get("enabled", True) or not enabled_only)]
    return sorted(ps, key=lambda p: p.get("priority", 0))


def move_priority(store: dict, pid: str, direction: int) -> None:
    """Move a provider up (-1) or down (+1) within its category."""
    p = get_provider(store, pid)
    if not p:
        return
    peers = providers_in(store, p["category"], enabled_only=False)
    idx = next((i for i, q in enumerate(peers) if q["id"] == pid), None)
    if idx is None:
        return
    j = idx + direction
    if 0 <= j < len(peers):
        peers[idx], peers[j] = peers[j], peers[idx]
        for i, q in enumerate(peers):
            q["priority"] = i


def category_label(cat: str) -> str:
    return _CATEGORY_LABELS.get(cat, cat)
