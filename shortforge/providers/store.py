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

CATEGORIES = ("tts", "asr", "ocr", "llm", "vision", "audio_library")
_CATEGORY_LABELS = {
    "tts": "Voice / TTS", "asr": "ASR / transcription",
    "ocr": "OCR / text-in-image", "llm": "LLM (text & analysis)",
    "vision": "Vision / Analysis", "audio_library": "Audio library (music & SFX)",
}


def store_path() -> str:
    return os.environ.get("SHORTFORGE_PROVIDERS_FILE") or os.path.join(
        "config", "providers.local.json")


def load_store() -> dict:
    path = store_path()
    if not os.path.isfile(path):
        return {"providers": [], "credentials": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("providers", [])      # legacy flat entries (one model each)
        data.setdefault("credentials", [])    # R6: a credential holds many models
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("providers store unreadable (%s); starting empty", e)
        return {"providers": [], "credentials": []}


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


def normalize_base(url: str) -> str:
    """Strip any pasted API path (…/chat/completions) back to the base, so a
    full endpoint pasted into a URL field is stored clean (never doubled later)."""
    try:
        from ..llm import normalize_base_url
        return normalize_base_url(url)
    except Exception:  # noqa: BLE001 - never block a save on this
        return (url or "").strip().rstrip("/")


def add_provider(store: dict, *, name: str, category: str, base_url: str = "",
                 api_key: str = "", model: str = "", voice: str = "") -> dict:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category '{category}'")
    prov = {
        "id": uuid.uuid4().hex[:8],
        "name": name.strip() or "provider",
        "category": category,
        "base_url": normalize_base(base_url),
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
    if fields.get("base_url") is not None:
        fields["base_url"] = normalize_base(fields["base_url"])
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


# --------------------------------------------------------------------------- #
# R6 — multi-model-per-credential schema
#
# One *credential* (base_url + api_key) holds many *models*, each with its own
# category, enable toggle, priority and detected capabilities. Runtime code never
# touches this shape directly: ``models_in(store, category)`` flattens both the
# new credential-models AND the legacy flat ``providers`` list into the same
# per-model dict the consumers already expect — so nothing downstream changed.
# --------------------------------------------------------------------------- #

def credentials(store: dict) -> list[dict]:
    return store.setdefault("credentials", [])


def add_credential(store: dict, *, name: str, base_url: str = "", api_key: str = "",
                   api_shape: str = "unknown") -> dict:
    cred = {
        "id": uuid.uuid4().hex[:8],
        "name": name.strip() or "credential",
        "base_url": normalize_base(base_url),
        "api_key": api_key.strip(),
        "api_shape": api_shape,
        "credit_note": "",          # R5: promotional-credit / conversion-rate note
        "enabled": True,
        "models": [],               # list of model dicts (see add_model)
        "available_models": [],     # ids fetched from /v1/models (for the picker)
        "last_fetch": None,         # BUG4: {status,url,body} of the last /models call
        "last_test": None,
    }
    credentials(store).append(cred)
    return cred


def get_credential(store: dict, cid: str) -> dict | None:
    return next((c for c in store.get("credentials", []) if c["id"] == cid), None)


def update_credential(store: dict, cid: str, **fields) -> dict | None:
    c = get_credential(store, cid)
    if c is None:
        return None
    if fields.get("base_url") is not None:
        fields["base_url"] = normalize_base(fields["base_url"])
    c.update({k: v for k, v in fields.items() if v is not None})
    return c


def delete_credential(store: dict, cid: str) -> None:
    store["credentials"] = [c for c in store.get("credentials", []) if c["id"] != cid]


def add_model(store: dict, cid: str, *, model: str, category: str, voice: str = "",
              display_name: str = "") -> dict:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category '{category}'")
    cred = get_credential(store, cid)
    if cred is None:
        raise ValueError(f"no such credential '{cid}'")
    m = {
        "id": uuid.uuid4().hex[:8],
        # BUG2: the exact API model id, stored verbatim — never truncated or
        # auto-generated. A friendly label lives in a SEPARATE field.
        "model": (model or "").strip(),
        "display_name": display_name.strip(),
        "category": category,
        "voice": voice.strip(),
        "enabled": True,
        "priority": len(models_in(store, category, enabled_only=False)),
        "capabilities": {},
        "api_shape": "",            # falls back to the credential's shape
        "voices": [],
        "last_test": None,
    }
    cred.setdefault("models", []).append(m)
    return m


def _find_model(store: dict, mid: str) -> tuple[dict | None, dict | None]:
    for cred in store.get("credentials", []):
        for m in cred.get("models", []):
            if m["id"] == mid:
                return cred, m
    return None, None


def get_model(store: dict, mid: str) -> dict | None:
    return _find_model(store, mid)[1]


def update_model(store: dict, mid: str, **fields) -> dict | None:
    _, m = _find_model(store, mid)
    if m is None:
        return None
    m.update({k: v for k, v in fields.items() if v is not None})
    return m


def delete_model(store: dict, mid: str) -> None:
    cred, m = _find_model(store, mid)
    if cred and m:
        cred["models"] = [x for x in cred["models"] if x["id"] != mid]


def _category_records(store: dict, category: str) -> list[tuple[dict | None, dict]]:
    """Raw (credential|None, model|provider) records in a category, by priority.

    ``credential`` is None for legacy flat ``providers`` entries. The second item
    is the live dict, so callers can write ``priority`` back through it.
    """
    out: list[tuple[dict | None, dict]] = []
    for cred in store.get("credentials", []):
        for m in cred.get("models", []):
            if m.get("category") == category:
                out.append((cred, m))
    for p in store.get("providers", []):
        if p.get("category") == category:
            out.append((None, p))
    out.sort(key=lambda t: t[1].get("priority", 0))
    return out


def _flatten(cred: dict | None, m: dict) -> dict:
    if cred is None:            # legacy provider record is already the right shape
        return dict(m)
    return {
        "id": m["id"],
        "credential_id": cred["id"],
        "name": (f"{cred.get('name', '')} · {m.get('model', '')}").strip(" ·"),
        "category": m.get("category"),
        "base_url": cred.get("base_url", ""),
        "api_key": cred.get("api_key", ""),
        "api_shape": m.get("api_shape") or cred.get("api_shape") or "unknown",
        "model": m.get("model", ""),
        "voice": m.get("voice", ""),
        "voices": m.get("voices", []),
        "capabilities": m.get("capabilities", {}),
        "enabled": bool(m.get("enabled", True) and cred.get("enabled", True)),
        "priority": m.get("priority", 0),
        "last_test": m.get("last_test"),
        "credit_note": cred.get("credit_note", ""),
    }


def models_in(store: dict, category: str, *, enabled_only: bool = True) -> list[dict]:
    """Every model in ``category`` — credential-models + legacy providers — as flat
    per-model dicts in priority order. The one accessor runtime code should use."""
    out = [_flatten(c, m) for c, m in _category_records(store, category)]
    if enabled_only:
        out = [p for p in out if p.get("enabled", True)]
    return out


def move_model_priority(store: dict, category: str, mid: str, direction: int) -> None:
    """Reorder a model within its category (across credentials + legacy)."""
    recs = _category_records(store, category)
    idx = next((i for i, (_, m) in enumerate(recs) if m["id"] == mid), None)
    if idx is None:
        return
    j = idx + direction
    if 0 <= j < len(recs):
        recs[idx], recs[j] = recs[j], recs[idx]
        for i, (_, m) in enumerate(recs):
            m["priority"] = i
