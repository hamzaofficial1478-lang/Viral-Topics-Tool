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
        data.setdefault("tasks", {})          # R1: per-task provider bindings
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("providers store unreadable (%s); starting empty", e)
        return {"providers": [], "credentials": [], "tasks": {}}


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
              display_name: str = "", tier: str = "unknown") -> dict:
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
        "tier": tier if tier in TIERS else "unknown",   # R2 cost-tier: free/paid/unknown
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
        d = dict(m)
        d.setdefault("tier", "unknown")
        d.setdefault("display_name", "")
        return d
    return {
        "id": m["id"],
        "credential_id": cred["id"],
        "name": (f"{cred.get('name', '')} · {m.get('model', '')}").strip(" ·"),
        "display_name": m.get("display_name", ""),
        "category": m.get("category"),
        "base_url": cred.get("base_url", ""),
        "api_key": cred.get("api_key", ""),
        "api_shape": m.get("api_shape") or cred.get("api_shape") or "unknown",
        "model": m.get("model", ""),
        "voice": m.get("voice", ""),
        "voices": m.get("voices", []),
        "tier": m.get("tier", "unknown"),
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


# --------------------------------------------------------------------------- #
# R1 — per-task provider binding   +   R2 — cost-tier guard
#
# The 14 pipeline tasks from the REVISION 2 routing matrix. Each binds its own
# primary/secondary/fallback model, an enable toggle, and (unless free_only) a
# paid_allowed toggle. free_only tasks (frame-scoring, OCR, and the free-tool
# tasks) can NEVER be flipped to paid — the guard hard-fails instead of spending.
# --------------------------------------------------------------------------- #

TASKS = (
    {"key": "asr",                 "label": "Transcription (ASR)",        "cats": ("asr",),                "free_only": False, "on": True},
    {"key": "hook_detection",      "label": "Hook detection ⭐",           "cats": ("llm", "vision"),       "free_only": False, "on": True},
    {"key": "clip_completeness",   "label": "Clip completeness",          "cats": ("llm",),                "free_only": False, "on": True},
    {"key": "emotion_labelling",   "label": "Emotion labelling",          "cats": ("llm",),                "free_only": False, "on": True},
    {"key": "dub_translation",     "label": "Dub translation",            "cats": ("llm",),                "free_only": False, "on": True},
    {"key": "caption_translation", "label": "Caption translation",        "cats": ("llm",),                "free_only": False, "on": True},
    {"key": "vision_scoring",      "label": "Vision / frame scoring",     "cats": ("vision",),             "free_only": True,  "on": True},
    {"key": "ocr",                 "label": "OCR / burned-in captions",   "cats": ("ocr", "vision"),       "free_only": True,  "on": True},
    {"key": "metadata",            "label": "Metadata (title/desc/tags)", "cats": ("llm",),                "free_only": False, "on": True},
    {"key": "tts_quality",         "label": "TTS — quality tier",         "cats": ("tts",),                "free_only": False, "on": True},
    {"key": "tts_volume",          "label": "TTS — volume tier",          "cats": ("tts",),                "free_only": False, "on": True},
    {"key": "voice_cloning",       "label": "Voice cloning",              "cats": ("tts",),                "free_only": False, "on": True},
    {"key": "lip_sync",            "label": "Lip sync",                   "cats": ("vision",),             "free_only": True,  "on": False},
    {"key": "audio_enhance",       "label": "Source audio enhancement",   "cats": ("audio_library", "asr"),"free_only": True,  "on": False},
)
_TASK_BY_KEY = {t["key"]: t for t in TASKS}

TIERS = ("unknown", "free", "paid")


def task_meta(key: str) -> dict | None:
    return _TASK_BY_KEY.get(key)


def tasks(store: dict) -> dict:
    return store.setdefault("tasks", {})


def get_task_binding(store: dict, key: str) -> dict:
    meta = _TASK_BY_KEY.get(key, {})
    t = tasks(store).get(key, {})
    return {
        "primary": t.get("primary", ""),
        "secondary": t.get("secondary", ""),
        "fallback": t.get("fallback", ""),
        "enabled": t.get("enabled", meta.get("on", True)),
        # R2: free_only tasks are paid_allowed=false, always.
        "paid_allowed": False if meta.get("free_only") else t.get("paid_allowed", True),
        "free_only": bool(meta.get("free_only")),
    }


def set_task_binding(store: dict, key: str, **fields) -> dict:
    if key not in _TASK_BY_KEY:
        raise ValueError(f"unknown task '{key}'")
    b = tasks(store).setdefault(key, {})
    for k, v in fields.items():
        if v is not None:
            b[k] = v
    if _TASK_BY_KEY[key].get("free_only"):
        b["paid_allowed"] = False          # R2: cannot be enabled
    return b


def resolve_task(store: dict, key: str) -> list[dict]:
    """Ordered available models for a task (primary→secondary→fallback).

    Explicit bindings win; if none are set, falls back to the task categories'
    priority order. Honours enable toggles and the free-only guard (R2): a
    free_only task only ever yields ``tier == 'free'`` models.
    """
    meta = _TASK_BY_KEY.get(key, {})
    b = get_task_binding(store, key)
    if not b["enabled"]:
        return []
    index: dict[str, dict] = {}
    ordered: list[dict] = []
    for cat in meta.get("cats", ()):
        for m in models_in(store, cat, enabled_only=True):
            if m["id"] not in index:
                index[m["id"]] = m
                ordered.append(m)
    explicit = [index[i] for i in (b["primary"], b["secondary"], b["fallback"])
                if i and i in index]
    chain = explicit or ordered
    if meta.get("free_only"):
        chain = [m for m in chain if m.get("tier") == "free"]
    return chain


def task_paid_violation(store: dict, key: str) -> str:
    """R2 — hard-fail message for a free_only task that has models available but
    none marked ``free`` (which would force paid spend). '' when clear."""
    meta = _TASK_BY_KEY.get(key, {})
    if not meta.get("free_only"):
        return ""
    any_available = any(models_in(store, c, enabled_only=True) for c in meta["cats"])
    if any_available and not resolve_task(store, key):
        cats = " / ".join(meta["cats"])
        return (f"Task '{meta['label']}' is FREE-only (paid_allowed=false) but no "
                f"provider marked 'free' is available. Mark a free {cats} model as free "
                f"tier (or add one) — refusing to spend on high-volume work.")
    return ""
