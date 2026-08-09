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
    """Write atomically (tmp + os.replace) — every credential lives here, so a
    power cut mid-write must never truncate/corrupt it (same discipline as
    queue.py and lifecycle.py's state file)."""
    from ..utils import write_json_atomic
    path = store_path()
    # Shared writer: a per-writer temp name, so two saves can never share a
    # scratch file and rename each other's half-written output into place.
    # mkstemp already creates at 0600 — keys live here, so re-assert it after
    # the rename rather than trusting the umask.
    write_json_atomic(path, store)
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
    if fields.get("api_key") is not None:
        fields["api_key"] = str(fields["api_key"]).strip()
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
        "auth_style": "bearer",     # bearer | x-api-key | token | custom (per-gateway auth shape)
        "auth_header_name": "",     # header name when auth_style == custom
        "timeout": 0,               # per-call HTTP timeout (s); 0 = inherit the task default
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
    if fields.get("api_key") is not None:
        fields["api_key"] = str(fields["api_key"]).strip()   # no stray whitespace/newline
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
        "base_url": (cred.get("base_url", "") or "").strip(),
        "api_key": (cred.get("api_key", "") or "").strip(),   # key clean at the single load point
        "api_shape": m.get("api_shape") or cred.get("api_shape") or "unknown",
        "model": m.get("model", ""),
        "voice": m.get("voice", ""),
        "voices": m.get("voices", []),
        "tier": m.get("tier", "unknown"),
        "auth_style": cred.get("auth_style", "bearer"),
        "auth_header_name": cred.get("auth_header_name", ""),
        "timeout": int(cred.get("timeout", 0) or 0),   # 0 = inherit task default
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

# ``timeout`` is the default per-call HTTP timeout (seconds). Hook detection is
# one long batched reasoning call per video, so it needs minutes, not 60s.
TASKS = (
    {"key": "asr",                 "label": "Transcription (ASR)",        "cats": ("asr",),                "free_only": False, "on": True,  "timeout": 600},
    {"key": "hook_detection",      "label": "Hook detection ⭐",           "cats": ("llm", "vision"),       "free_only": False, "on": True, "fusion": True, "timeout": 600},
    {"key": "clip_completeness",   "label": "Clip completeness",          "cats": ("llm",),                "free_only": False, "on": True,  "timeout": 300},
    {"key": "emotion_labelling",   "label": "Emotion labelling",          "cats": ("llm",),                "free_only": False, "on": True, "batched": True, "timeout": 300},
    {"key": "dub_translation",     "label": "Dub translation",            "cats": ("llm",),                "free_only": False, "on": True,  "timeout": 180},
    {"key": "caption_translation", "label": "Caption translation",        "cats": ("llm",),                "free_only": False, "on": True,  "timeout": 180},
    {"key": "vision_scoring",      "label": "Vision / frame scoring",     "cats": ("vision",),             "free_only": True,  "on": True,  "timeout": 300},
    {"key": "ocr",                 "label": "OCR / burned-in captions",   "cats": ("ocr", "vision"),       "free_only": True,  "on": True,  "timeout": 300},
    {"key": "metadata",            "label": "Metadata (title/desc/tags)", "cats": ("llm",),                "free_only": False, "on": True,  "timeout": 180},
    {"key": "tts_quality",         "label": "TTS — quality tier",         "cats": ("tts",),                "free_only": False, "on": True},
    {"key": "tts_volume",          "label": "TTS — volume tier",          "cats": ("tts",),                "free_only": False, "on": True},
    {"key": "voice_cloning",       "label": "Voice cloning",              "cats": ("tts",),                "free_only": False, "on": True},
    {"key": "lip_sync",            "label": "Lip sync",                   "cats": ("vision",),             "free_only": True,  "on": False},
    {"key": "audio_enhance",       "label": "Source audio enhancement",   "cats": ("audio_library", "asr"),"free_only": True,  "on": False},
)
_TASK_BY_KEY = {t["key"]: t for t in TASKS}

TIERS = ("unknown", "free", "paid")

# Always-available local backends (no credential, no key). They are selectable in
# task routing so the operator can bind e.g. TTS volume -> edge-tts, ASR -> local
# Whisper, without inventing a fake credential. They are free by definition.
_BUILTINS = (
    {"id": "builtin:edge-tts", "name": "edge-tts (local)", "display_name": "edge-tts (local)",
     "model": "edge", "category": "tts", "tier": "free", "api_shape": "edge",
     "base_url": "", "api_key": "", "voice": "", "voices": [], "capabilities": {},
     "enabled": True, "priority": 10000, "builtin": True, "last_test": None, "credit_note": ""},
    {"id": "builtin:local-whisper", "name": "local Whisper (small)",
     "display_name": "local Whisper", "model": "small", "category": "asr", "tier": "free",
     "api_shape": "local", "base_url": "", "api_key": "", "voice": "", "voices": [],
     "capabilities": {}, "enabled": True, "priority": 10000, "builtin": True,
     "last_test": None, "credit_note": ""},
)


def builtin_models(category: str) -> list[dict]:
    """Built-in local backends (edge-tts, local Whisper) for a category — fresh
    copies so callers can't mutate the templates."""
    return [dict(m) for m in _BUILTINS if m["category"] == category]


def routable_models(store: dict, category: str) -> list[dict]:
    """Everything bindable to a task in ``category``: stored models (any enable
    state) + the built-in local backends. Used to populate the routing UI."""
    return models_in(store, category, enabled_only=False) + builtin_models(category)


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
        for m in builtin_models(cat):        # local backends as last-resort fallbacks
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


def is_fusion_task(key: str) -> bool:
    return bool(_TASK_BY_KEY.get(key, {}).get("fusion"))


def is_batched_task(key: str) -> bool:
    return bool(_TASK_BY_KEY.get(key, {}).get("batched"))


def task_timeout(key: str, default: int = 120) -> int:
    """Default per-call HTTP timeout (seconds) for a task."""
    return int(_TASK_BY_KEY.get(key, {}).get("timeout") or default)


def _candidate_index(store: dict, cats) -> tuple[dict, list]:
    index: dict[str, dict] = {}
    ordered: list[dict] = []
    for cat in cats:
        for m in models_in(store, cat, enabled_only=True):
            if m["id"] not in index:
                index[m["id"]] = m
                ordered.append(m)
        for m in builtin_models(cat):
            if m["id"] not in index:
                index[m["id"]] = m
                ordered.append(m)
    return index, ordered


def resolve_fusion(store: dict, key: str) -> dict:
    """For a FUSION task (item 1): the secondary is a CONTRIBUTOR that runs
    alongside the primary and whose score is combined — not a failover fallback.

    Returns {"contributors": [...], "fallback": [...]}. ``contributors`` run in
    parallel and their scores fuse; ``fallback`` is tried only on error. If no
    explicit binding, contributors default to the top model of each category
    (e.g. a transcript LLM + a vision frame scorer).
    """
    meta = _TASK_BY_KEY.get(key, {})
    b = get_task_binding(store, key)
    if not b["enabled"]:
        return {"contributors": [], "fallback": []}
    index, ordered = _candidate_index(store, meta.get("cats", ()))
    contributors = [index[i] for i in (b["primary"], b["secondary"]) if i and i in index]
    if not contributors:
        # one model per distinct category, in priority order (transcript + frames)
        by_cat: dict[str, dict] = {}
        for m in ordered:
            by_cat.setdefault(m.get("category"), m)
        contributors = list(by_cat.values())
    if meta.get("free_only"):
        contributors = [m for m in contributors if m.get("tier") == "free"]
    fb = index.get(b["fallback"])
    return {"contributors": contributors, "fallback": [fb] if fb else []}


# --------------------------------------------------------------------------- #
# Legacy → credential migration (item 3): one config path only.
# --------------------------------------------------------------------------- #

def _credential_name_for(base_url: str, sample_name: str = "") -> str:
    b = (base_url or "").lower()
    for needle, label in (("nvidia", "NVIDIA build"), ("elevenlabs", "ElevenLabs"),
                          ("vercel", "Vercel AI Gateway"), ("forge", "Forge AI"),
                          ("openai", "OpenAI")):
        if needle in b:
            return label
    try:
        from urllib.parse import urlparse
        host = urlparse(base_url).netloc
    except Exception:  # noqa: BLE001
        host = ""
    return host or sample_name or "Imported"


def migrate_legacy(store: dict) -> int:
    """Move legacy flat ``providers`` into credentials (grouped by base_url+key),
    preserving each entry's id, category, priority, enable state, capabilities and
    tier — so existing task bindings (which reference ids) keep resolving. The
    NVIDIA endpoint is named "NVIDIA build". Idempotent; returns entries moved."""
    legacy = store.get("providers", [])
    if not legacy:
        return 0
    creds = credentials(store)
    moved = 0
    for p in legacy:
        base = normalize_base(p.get("base_url", ""))
        key = p.get("api_key", "")
        cred = next((c for c in creds if c.get("base_url") == base
                     and c.get("api_key") == key), None)
        if cred is None:
            cred = add_credential(store, name=_credential_name_for(base, p.get("name", "")),
                                  base_url=base, api_key=key,
                                  api_shape=p.get("api_shape", "unknown"))
        cred.setdefault("models", []).append({
            "id": p.get("id") or uuid.uuid4().hex[:8],   # keep id → bindings stay valid
            "model": p.get("model", ""),
            "display_name": p.get("name", ""),
            "category": p.get("category", "llm"),
            "voice": p.get("voice", ""),
            "enabled": p.get("enabled", True),
            "tier": p.get("tier", "unknown"),
            "priority": p.get("priority", 0),
            "capabilities": p.get("capabilities", {}),
            "api_shape": p.get("api_shape", ""),
            "voices": p.get("voices", []),
            "last_test": p.get("last_test"),
        })
        moved += 1
    store["providers"] = []
    return moved
