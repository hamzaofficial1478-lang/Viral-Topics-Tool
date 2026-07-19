"""Settings screen — visual API-provider manager (no .env editing needed).

Add unlimited providers, auto-detect their capabilities ("Test & Detect"), run a
real sample call ("Test", with an audio player for TTS), order them for failover,
enable/disable, and see everything in one overview table. Everything is saved to
the gitignored provider store; keys are masked after saving and never logged.
"""

from __future__ import annotations

import os
import tempfile

import streamlit as st

from ..providers import store as S
from ..providers import detect as D
from ..providers import test_tts_provider, capability_warnings

_CATS = list(S.CATEGORIES)


def _load():
    if "prov_store" not in st.session_state:
        st.session_state.prov_store = S.load_store()
    return st.session_state.prov_store


def _persist(store):
    S.save_store(store)
    st.session_state.prov_store = store


def _caps_summary(p: dict) -> str:
    c = p.get("capabilities") or {}
    if not c:
        return "not detected"
    def m(v):
        return "✓" if v is True else "✗" if v is False else "?"
    langs = c.get("languages") or []
    return (f"SSML {m(c.get('ssml'))} · emotion {m(c.get('emotion'))} · "
            f"clone {m(c.get('cloning'))}"
            + (f" · langs {','.join(langs[:6])}" if langs else ""))


def render() -> None:
    st.header("⚙️ API providers")
    st.caption("Add your voice / LLM / analysis / audio providers here. Click "
               "**Test & Detect** to auto-discover what each one can do — no manual "
               "config needed. Keys are stored locally (gitignored) and masked after saving.")

    store = _load()

    for w in capability_warnings(store):
        st.warning(w)

    # --- overview table ---
    if store["providers"]:
        rows = []
        for p in store["providers"]:
            lt = p.get("last_test") or {}
            rows.append({
                "Name": p["name"], "Category": S.category_label(p["category"]),
                "Status": ("✓" if lt.get("ok") else "✗" if lt else "—"),
                "Capabilities": _caps_summary(p),
                "Priority": p.get("priority", 0),
                "Enabled": "on" if p.get("enabled", True) else "off",
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)

    st.divider()

    # --- per-provider cards ---
    for cat in _CATS:
        cat_ps = S.providers_in(store, cat, enabled_only=False)
        if not cat_ps:
            continue
        st.subheader(S.category_label(cat))
        for i, p in enumerate(cat_ps):
            _render_card(store, p, i, len(cat_ps))

    st.divider()
    _render_add_form(store)


def _render_card(store, p, idx, count):
    pid = p["id"]
    with st.expander(f"{p['name']}  ·  {_caps_summary(p)}", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("Name", p["name"], key=f"n_{pid}")
            base = st.text_input("Base URL", p.get("base_url", ""), key=f"b_{pid}")
            key_in = st.text_input(f"API key (saved: {S.masked(p.get('api_key'))})",
                                   "", type="password", key=f"k_{pid}",
                                   placeholder="leave blank to keep current")
        with c2:
            model = st.text_input("Model", p.get("model", ""), key=f"m_{pid}")
            voices = p.get("voices") or []
            if voices:
                cur = p.get("voice", "")
                opts = ([cur] if cur and cur not in voices else []) + voices
                voice = st.selectbox("Voice", opts, key=f"v_{pid}")
            else:
                voice = st.text_input("Voice / voice ID", p.get("voice", ""), key=f"vt_{pid}")
            enabled = st.checkbox("Enabled", p.get("enabled", True), key=f"e_{pid}")

        # detected capabilities detail
        caps = p.get("capabilities") or {}
        if caps:
            st.markdown("**Detected:** " + _caps_summary(p)
                        + (f"  ·  emotion params: `{', '.join(caps.get('emotion_params', []))}`"
                           if caps.get("emotion_params") else "")
                        + f"  ·  API shape: `{p.get('api_shape','unknown')}`")
            for note in caps.get("notes", []):
                st.caption("• " + note)

        b = st.columns(6)
        if b[0].button("💾 Save", key=f"sv_{pid}"):
            fields = {"name": name, "base_url": base, "model": model,
                      "voice": voice, "enabled": enabled}
            if key_in:
                fields["api_key"] = key_in
            S.update_provider(store, pid, **fields)
            _persist(store)
            st.success("Saved.")
            st.rerun()
        if b[1].button("🔍 Test & Detect", key=f"td_{pid}"):
            if key_in:
                S.update_provider(store, pid, api_key=key_in)
            S.update_provider(store, pid, name=name, base_url=base, model=model)
            cur = S.get_provider(store, pid)
            with st.spinner("Probing provider…"):
                res = D.detect(cur["category"], cur.get("base_url", ""),
                               cur.get("api_key", ""), cur.get("model", ""))
            S.update_provider(store, pid, capabilities=res, api_shape=res.get("api_shape"),
                              voices=res.get("voices", []), models=res.get("models", []))
            _persist(store)
            st.rerun()
        if p["category"] == "tts" and b[2].button("🔊 Test", key=f"ts_{pid}"):
            if key_in:
                S.update_provider(store, pid, api_key=key_in)
            cur = S.get_provider(store, pid)
            out = tempfile.mktemp(suffix=".wav")
            with st.spinner("Synthesizing sample…"):
                r = test_tts_provider(cur, out)
            S.update_provider(store, pid, last_test=r)
            _persist(store)
            if r["ok"]:
                st.success(r["detail"])
                st.session_state[f"audio_{pid}"] = out
            else:
                st.error(r["detail"])
        if b[3].button("⬆", key=f"up_{pid}", disabled=idx == 0):
            S.move_priority(store, pid, -1)
            _persist(store)
            st.rerun()
        if b[4].button("⬇", key=f"dn_{pid}", disabled=idx == count - 1):
            S.move_priority(store, pid, +1)
            _persist(store)
            st.rerun()
        if b[5].button("🗑", key=f"del_{pid}"):
            S.delete_provider(store, pid)
            _persist(store)
            st.rerun()

        audio = st.session_state.get(f"audio_{pid}")
        if audio and os.path.isfile(audio):
            st.audio(audio)


def _render_add_form(store):
    st.subheader("➕ Add a provider")
    with st.form("add_provider", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("Provider name", placeholder="e.g. ElevenLabs, OpenAI")
            category = st.selectbox("Category", _CATS,
                                    format_func=S.category_label)
            base = st.text_input("Base URL", placeholder="https://api.provider.com/v1")
        with c2:
            key = st.text_input("API key", type="password")
            model = st.text_input("Model / voice ID (optional)")
        if st.form_submit_button("Add"):
            if not name:
                st.error("Name is required.")
            else:
                S.add_provider(store, name=name, category=category, base_url=base,
                               api_key=key, model=model)
                _persist(store)
                st.success(f"Added {name}. Open its card to Test & Detect.")
                st.rerun()
