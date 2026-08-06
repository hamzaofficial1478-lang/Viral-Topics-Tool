"""Settings screen — visual API-provider manager (no .env editing needed).

Add unlimited providers, auto-detect their capabilities ("Test & Detect"), run a
real sample call ("Test", with an audio player for TTS), order them for failover,
enable/disable, and see everything in one overview table. Everything is saved to
the gitignored provider store; keys are masked after saving and never logged.
"""

from __future__ import annotations

import json
import os
import tempfile

import streamlit as st

from ..providers import store as S
from ..providers import detect as D
from ..providers import test_tts_provider, capability_warnings

_CATS = list(S.CATEGORIES)

# A settings backup dropped in the project root is auto-offered on first launch.
BACKUP_FILENAME = "shortforge-settings.json"


def _load():
    if "prov_store" not in st.session_state:
        st.session_state.prov_store = S.load_store()
    return st.session_state.prov_store


def _persist(store):
    S.save_store(store)
    st.session_state.prov_store = store


def _m(v):
    return "✓" if v is True else "✗" if v is False else "?"


def _store_is_empty(store: dict) -> bool:
    """Nothing configured yet — no credentials and no legacy providers."""
    return not S.credentials(store) and not store.get("providers")


def _valid_store(data) -> bool:
    """A plausible ShortForge settings file (not arbitrary JSON)."""
    return isinstance(data, dict) and any(
        k in data for k in ("credentials", "providers", "tasks"))


def _import_store(data) -> None:
    """Replace the whole provider store with an imported backup (keys included)."""
    if not _valid_store(data):
        raise ValueError("not a ShortForge settings file "
                         "(expected credentials / providers / tasks)")
    data.setdefault("providers", [])
    data.setdefault("credentials", [])
    data.setdefault("tasks", {})
    _persist(data)


def _render_autodetect_import(store: dict) -> None:
    """First-launch convenience: if a backup is sitting in the project root and
    nothing is configured yet, offer to restore it in one click (item 3)."""
    if st.session_state.get("autoimport_dismissed"):
        return
    if not os.path.isfile(BACKUP_FILENAME) or not _store_is_empty(store):
        return
    st.info(f"Found **{BACKUP_FILENAME}** in the project folder and no providers are "
            "configured yet. Restore your saved credentials, models and task routing?")
    c1, c2 = st.columns(2)
    if c1.button(f"⬇️ Import {BACKUP_FILENAME}", type="primary", key="autoimport_yes"):
        try:
            with open(BACKUP_FILENAME, "r", encoding="utf-8") as f:
                _import_store(json.load(f))
            st.session_state.autoimport_dismissed = True
            st.success("Imported — your API keys and routing are restored.")
            st.rerun()
        except (json.JSONDecodeError, OSError, ValueError) as e:
            st.error(f"Could not import {BACKUP_FILENAME}: {e}")
    if c2.button("No thanks", key="autoimport_no"):
        st.session_state.autoimport_dismissed = True
        st.rerun()


def _render_telegram(store: dict) -> None:
    """Telegram progress messages: one per finished link, one when the queue ends."""
    from ..notify import test_telegram

    st.divider()
    st.subheader("📨 Telegram notifications")
    st.caption("Get a message when each link finishes and when the whole queue is done — "
               "so you can leave it running and walk away.")
    with st.expander("How to get these two values", expanded=False):
        st.markdown(
            "1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts. "
            "It gives you a **bot token** like `123456:ABC-DEF...`.\n"
            "2. Send any message to your new bot (this is required — bots can't "
            "message you first).\n"
            "3. Message **@userinfobot** → it replies with your **chat id** (a number).\n"
            "4. Paste both below, Save, then press Test.")

    tg = store.get("telegram", {}) or {}
    token = st.text_input("Bot token", value=tg.get("bot_token", "") or "", type="password")
    chat_id = st.text_input("Chat id", value=str(tg.get("chat_id", "") or ""))

    if st.button("🩺 Diagnose connection (run this if Test times out)", width="stretch"):
        from ..netdiag import report
        with st.spinner("Checking DNS → TCP → TLS → HTTPS…"):
            st.code(report(timeout=10))

    c1, c2 = st.columns(2)
    if c1.button("💾 Save Telegram", width="stretch"):
        store["telegram"] = {"bot_token": token.strip() or None,
                             "chat_id": chat_id.strip() or None}
        _persist(store)
        st.success("Saved.")
    if c2.button("🔔 Send test message", width="stretch",
                 disabled=not (token.strip() and chat_id.strip())):
        ok, detail = test_telegram(token.strip(), chat_id.strip())
        (st.success if ok else st.error)(detail)


def _render_youtube_auth(store: dict) -> None:
    """YouTube auth: browser/cookies picker + Test + Update yt-dlp. Persisted to
    the store and applied to every download."""
    from ..config import Config
    from ..ingest import test_youtube_auth, update_ytdlp, ytdlp_version

    st.divider()
    st.subheader("📺 YouTube authentication")
    st.caption("YouTube increasingly blocks downloads with a \"confirm you're not a bot\" wall. "
               "Point ShortForge at the browser you're logged into YouTube with — those cookies "
               "attach to every download automatically.")

    ya = store.get("youtube_auth", {}) or {}
    browsers = ["none", "firefox", "chrome", "edge", "brave"]
    cur = ya.get("cookies_from_browser") or "firefox"
    browser = st.selectbox(
        "Cookies from browser", browsers,
        index=browsers.index(cur) if cur in browsers else 1,
        help="Reads the browser's own YouTube login cookies (yt-dlp --cookies-from-browser).")
    if browser == "firefox":
        st.caption("✅ Firefox is the most reliable on Windows.")
    elif browser in ("chrome", "edge"):
        st.caption("⚠️ Chrome/Edge encrypt and lock their cookie store while running — this "
                   "frequently breaks extraction. Prefer Firefox, or fully close the browser first.")
    cookies_file = st.text_input(
        "…or a cookies.txt file (optional)", value=ya.get("cookies_file", "") or "",
        help="A Netscape-format cookies.txt exported from your browser. Tried before browser cookies.")
    test_url = st.text_input("Test URL (paste one of your video links)", key="yt_test_url",
                             placeholder="https://www.youtube.com/watch?v=…")

    c1, c2 = st.columns(2)
    if c1.button("💾 Save authentication", width="stretch"):
        store["youtube_auth"] = {
            "cookies_from_browser": None if browser == "none" else browser,
            "cookies_file": cookies_file.strip() or None,
        }
        _persist(store)
        st.success("Saved — applied to every download.")
    if c2.button("🔎 Test authentication", width="stretch", disabled=not test_url.strip()):
        cfg = Config.load()
        cfg.override("ingest.cookies_from_browser", None if browser == "none" else browser)
        cfg.override("ingest.cookies", cookies_file.strip() or None)
        with st.spinner("Fetching metadata (no download) — trying every strategy…"):
            ok, detail = test_youtube_auth(test_url.strip(), cfg)
        (st.success if ok else st.error)("Reachable ✓" if ok else "No strategy worked ✗")
        st.code(detail or "(no result)")   # per-strategy: confirms whether your browser cookies work

    ver = ytdlp_version() or "not installed"
    st.caption(f"yt-dlp version: **{ver}** — extractors break often; keep it current.")
    if st.button("⬆️ Update yt-dlp"):
        with st.spinner("Running pip install -U yt-dlp…"):
            ok, detail = update_ytdlp()
        (st.success if ok else st.error)(detail)


def _render_backup_restore(store: dict) -> None:
    """Export/import the whole settings store so moving machines needs no re-entry."""
    st.divider()
    st.subheader("💾 Backup & restore settings")
    st.caption("Move to a new PC without re-entering anything: export one file with every "
               "credential, model, category, priority and task binding, then import it there.")
    st.warning("⚠️ The exported file contains your **API keys in plaintext**. Keep it "
               "private — do not commit it or share it.")
    payload = json.dumps(store, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button("⬇️ Export settings", data=payload, file_name=BACKUP_FILENAME,
                       mime="application/json", key="settings_export")
    up = st.file_uploader("⬆️ Import settings (replaces everything configured now)",
                          type="json", key="settings_import")
    if up is not None:
        sig = (up.name, up.size)
        if st.session_state.get("_last_import_sig") != sig:   # import each file once, no rerun loop
            st.session_state["_last_import_sig"] = sig
            try:
                _import_store(json.loads(up.getvalue().decode("utf-8")))
                st.success("Settings imported — credentials, models and task routing restored.")
                st.rerun()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
                st.error(f"Import failed: {e}")


def _caps_summary(p: dict) -> str:
    c = p.get("capabilities") or {}
    if not c:
        return "not detected"
    if p.get("category") in ("llm", "vision", "ocr"):
        # LLM/vision/OCR: no SSML/emotion/clone (TTS concepts).
        vis = c.get("multimodal")
        parts = [f"reachable {_m(c.get('reachable'))}",
                 f"responds {_m(c.get('model_responds'))}",
                 f"vision {_m(vis if vis in (True, False) else None)}",
                 f"shape {c.get('api_shape', '?')}"]
        if c.get("context_length"):
            parts.append(f"ctx {c['context_length']}")
        if c.get("status_code"):
            parts.append(f"HTTP {c['status_code']}")
        return " · ".join(parts)
    langs = c.get("languages") or []
    return (f"SSML {_m(c.get('ssml'))} · emotion {_m(c.get('emotion'))} · "
            f"clone {_m(c.get('cloning'))}"
            + (f" · langs {','.join(langs[:6])}" if langs else ""))


def render() -> None:
    st.header("⚙️ API providers")
    st.caption("Add a **credential** once (base URL + key), then **Fetch available "
               "models** and enable the ones you want — each with its own category, "
               "toggle and failover priority. Click **Test & Detect** to auto-discover "
               "what a model can do. Keys are stored locally (gitignored) and masked.")

    store = _load()

    _render_autodetect_import(store)

    for w in capability_warnings(store):
        st.warning(w)

    # --- overview table (credential-models + legacy, one row per model) ---
    rows = []
    for cat in _CATS:
        for p in S.models_in(store, cat, enabled_only=False):
            lt = p.get("last_test") or {}
            rows.append({
                "Model": p.get("name", "?"),
                "Category": S.category_label(p.get("category", "")),
                "Status": ("✓" if lt.get("ok") else "✗" if lt else "—"),
                "Capabilities": _caps_summary(p),
                "Priority": p.get("priority", 0),
                "Enabled": "on" if p.get("enabled", True) else "off",
            })
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)

    st.divider()

    # --- credentials (R6: one credential holds many models) ---
    st.subheader("🔑 Credentials")
    for cred in S.credentials(store):
        _render_credential(store, cred)
    _render_add_credential(store)

    # --- per-task routing (R1) + cost-tier guard (R2) ---
    st.divider()
    _render_task_routing(store)

    # --- Telegram progress notifications ---
    _render_telegram(store)

    # --- YouTube authentication (fix the bot wall on downloads) ---
    _render_youtube_auth(store)

    # --- backup / restore (move machines without re-entering keys) ---
    _render_backup_restore(store)

    # --- legacy migration (item 3): collapse to ONE config path ---
    legacy = store.get("providers", [])
    if legacy:
        st.divider()
        st.warning(f"{len(legacy)} legacy single-model provider(s) found from an older "
                   "version. Migrate them into credentials — keys, priorities, toggles and "
                   "task bindings are preserved, and the NVIDIA endpoint is named "
                   "'NVIDIA build'. After this there's only one config path (credentials).")
        if st.button(f"⬆️ Migrate {len(legacy)} legacy provider(s) into credentials"):
            n = S.migrate_legacy(store)
            _persist(store)
            st.success(f"Migrated {n} provider(s) into credentials.")
            st.rerun()


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
            cost1k = None
            if p["category"] == "tts":
                cur_cost = float((p.get("capabilities") or {}).get("cost_per_1k_chars", 0.0) or 0.0)
                cost1k = st.number_input("Cost per 1000 characters (USD)", min_value=0.0,
                                         value=cur_cost, step=0.01, format="%.4f", key=f"c1k_{pid}")

        # detected capabilities detail
        caps = p.get("capabilities") or {}
        if caps:
            extra = ""
            if p["category"] == "tts" and caps.get("emotion_params"):
                extra = f"  ·  emotion params: `{', '.join(caps['emotion_params'])}`"
            st.markdown("**Detected:** " + _caps_summary(p) + extra)
            # STEP 3: TTS language coverage (a MODEL property).
            if p["category"] == "tts":
                langs = caps.get("languages") or []
                if langs:
                    st.markdown(f"**Languages ({p.get('model') or 'this model'}):** "
                                + ", ".join(langs))
                ml = caps.get("model_languages") or {}
                if ml:
                    with st.expander("Language coverage per model"):
                        for mid, ls in sorted(ml.items()):
                            st.markdown(f"- **{mid}**: {', '.join(ls)}")
            for note in caps.get("notes", []):
                st.caption("• " + note)
            # Raw probe request/response (redacted) — makes failures diagnosable.
            if caps.get("request_excerpt") or caps.get("response_excerpt"):
                with st.expander("Raw probe request / response (keys redacted)"):
                    if caps.get("request_excerpt"):
                        st.code(caps["request_excerpt"])
                    if caps.get("response_excerpt"):
                        st.code(caps["response_excerpt"])

        b = st.columns(6)
        if b[0].button("💾 Save", key=f"sv_{pid}"):
            fields = {"name": name, "base_url": base, "model": model,
                      "voice": voice, "enabled": enabled}
            if key_in:
                fields["api_key"] = key_in
            S.update_provider(store, pid, **fields)
            if cost1k is not None:
                caps = dict(S.get_provider(store, pid).get("capabilities") or {})
                caps["cost_per_1k_chars"] = float(cost1k)
                S.update_provider(store, pid, capabilities=caps)
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
            # Preserve the operator-set price (not auto-detectable).
            prev_cost = (cur.get("capabilities") or {}).get("cost_per_1k_chars")
            if prev_cost is not None:
                res["cost_per_1k_chars"] = prev_cost
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


# --------------------------------------------------------------------------- #
# R6 — credential (many models) widgets
# --------------------------------------------------------------------------- #

def _render_add_credential(store):
    with st.expander("➕ Add a credential (one key, many models)", expanded=not S.credentials(store)):
        with st.form("add_cred", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                name = st.text_input("Credential name",
                                     placeholder="e.g. AgentRouter, Forge AI, NVIDIA")
                base = st.text_input("Base URL", placeholder="https://agentrouter.org/v1")
            with c2:
                key = st.text_input("API key", type="password")
            if st.form_submit_button("Add credential"):
                if not name:
                    st.error("Name is required.")
                else:
                    S.add_credential(store, name=name, base_url=base, api_key=key)
                    _persist(store)
                    st.success(f"Added {name}. Open it, Fetch available models, then add models.")
                    st.rerun()


def _render_credential(store, cred):
    cid = cred["id"]
    n = len(cred.get("models", []))
    header = f"🔑 {cred['name']}  ·  {cred.get('base_url', '') or 'no URL'}  ·  {n} model(s)"
    with st.expander(header, expanded=False):
        _AUTH_STYLES = ["bearer", "x-api-key", "token", "custom"]
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("Name", cred["name"], key=f"cn_{cid}")
            base = st.text_input("Base URL", cred.get("base_url", ""), key=f"cb_{cid}")
            cur_style = cred.get("auth_style", "bearer")
            auth_style = st.selectbox(
                "Auth header", _AUTH_STYLES,
                index=_AUTH_STYLES.index(cur_style) if cur_style in _AUTH_STYLES else 0,
                key=f"cas_{cid}",
                help="How the key is sent. bearer = Authorization: Bearer <key> (default); "
                     "x-api-key = x-api-key: <key>; token = Authorization: <key> (no prefix); "
                     "custom = your own header name. Forge 'fg-' consumer keys may need x-api-key.")
            auth_header_name = ""
            if auth_style == "custom":
                auth_header_name = st.text_input("Custom header name",
                                                 cred.get("auth_header_name", ""), key=f"cahn_{cid}")
        with c2:
            key_in = st.text_input(f"API key (saved: {S.masked(cred.get('api_key'))})",
                                   "", type="password", key=f"ck_{cid}",
                                   placeholder="leave blank to keep current")
            credit = st.text_input("Credit / balance note", cred.get("credit_note", ""),
                                   key=f"ccr_{cid}",
                                   help="Promotional credits or a non-standard conversion "
                                        "rate (some gateways bill ~4x). Shown in cost reports.")
            timeout = st.number_input(
                "Request timeout (s, 0 = task default)", min_value=0,
                value=int(cred.get("timeout", 0) or 0), step=30, key=f"cto_{cid}",
                help="Per-call HTTP timeout. 0 inherits the task default (hook detection "
                     "600s). Raise for slow reasoning models on long batched calls.")

        # BUG1: show exactly what was stored + the endpoint that will be hit, so a
        # pasted full URL (…/chat/completions) is visibly normalized back to base.
        stored = cred.get("base_url", "")
        if stored:
            from ..llm import normalize_chat_url
            st.caption(f"Stored base: `{stored}` → chat endpoint `{normalize_chat_url(stored)}`")

        bb = st.columns(3)
        if bb[0].button("💾 Save", key=f"csv_{cid}"):
            fields = {"name": name, "base_url": base, "credit_note": credit,
                      "auth_style": auth_style, "auth_header_name": auth_header_name,
                      "timeout": int(timeout)}
            if key_in:
                fields["api_key"] = key_in
            S.update_credential(store, cid, **fields)
            _persist(store)
            st.success("Saved.")
            st.rerun()
        if bb[1].button("📥 Fetch available models", key=f"cf_{cid}"):
            if key_in:
                S.update_credential(store, cid, api_key=key_in)
            S.update_credential(store, cid, name=name, base_url=base)
            cur = S.get_credential(store, cid)
            with st.spinner("Querying /models…"):
                diag = D.fetch_models_diag(cur.get("base_url", ""), cur.get("api_key", ""))
            S.update_credential(store, cid, available_models=diag["models"],
                                last_fetch={"status": diag["status"], "url": diag["url"],
                                            "body": diag["body"], "error": diag["error"]})
            _persist(store)
            st.rerun()
        if bb[2].button("🗑 Delete credential", key=f"cdel_{cid}"):
            S.delete_credential(store, cid)
            _persist(store)
            st.rerun()

        # BUG4: fetch diagnostics — always show HTTP status + URL + body when a
        # fetch returned nothing, and remind that manual entry is available.
        lf = cred.get("last_fetch")
        if lf:
            if cred.get("available_models"):
                st.caption(f"✓ Fetched {len(cred['available_models'])} model(s) "
                           f"(HTTP {lf.get('status')}).")
            else:
                st.warning(f"Fetch returned no models — HTTP {lf.get('status')} at "
                           f"`{lf.get('url')}`. Add model ids by hand below (fetching is "
                           f"optional).")
                with st.expander("Raw /models response"):
                    st.code(f"GET {lf.get('url')}\nHTTP {lf.get('status')}\n"
                            f"{lf.get('error') or ''}\n\n{lf.get('body') or '(empty)'}")

        # add a model to this credential
        st.markdown("**Add a model**  — the exact API model id is stored and sent verbatim.")
        avail = cred.get("available_models") or []
        mc = st.columns([3, 2, 2, 2, 1])
        with mc[0]:
            pick = st.selectbox("From fetched list", ["(type below)"] + avail,
                                key=f"mp_{cid}") if avail else "(type below)"
            typed = st.text_input("…or exact model id", key=f"mt_{cid}",
                                  placeholder="e.g. meta/llama-3.2-11b-vision-instruct")
        with mc[1]:
            mcat = st.selectbox("Category", _CATS, format_func=S.category_label, key=f"mc_{cid}")
        with mc[2]:
            tier = st.selectbox("Cost tier", S.TIERS, key=f"mtier_{cid}",
                                help="Mark 'free' to let it run frame-scoring/OCR tasks "
                                     "(those refuse to spend). 'paid' for metered gateways.")
        with mc[3]:
            disp = st.text_input("Display name (optional)", key=f"md_{cid}",
                                 placeholder="friendly label — not sent to the API")
        with mc[4]:
            st.write("")
            if st.button("Add model", key=f"madd_{cid}"):
                model_id = typed.strip() or (pick if pick != "(type below)" else "")
                if model_id:
                    S.add_model(store, cid, model=model_id, category=mcat,
                                display_name=disp, tier=tier)
                    _persist(store)
                    st.rerun()
                else:
                    st.error("Pick or type a model id.")

        # existing models
        if cred.get("models"):
            st.markdown("**Models**")
            for m in cred["models"]:
                _render_model_row(store, cred, m)


def _render_task_routing(store):
    st.subheader("🎛️ Task routing")
    st.caption("Each pipeline task binds its own models (primary → secondary → "
               "fallback). Leave a slot on **(auto)** to use the category's priority "
               "order. 🔒 tasks are FREE-only — they refuse to spend, so give them a "
               "model marked *free* tier.")
    for meta in S.TASKS:
        _render_task_row(store, meta)


def _task_model_options(store, cats):
    """(id, label) options for a task's category models + built-in local backends,
    plus an (auto) sentinel."""
    opts = [("", "(auto by priority)")]
    seen = set()
    for cat in cats:
        for m in S.routable_models(store, cat):
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            tag = m.get("tier", "unknown")
            lbl = (m.get("display_name") or m.get("model") or m.get("name") or m["id"])
            opts.append((m["id"], f"{lbl}  [{tag}]"))
    return opts


def _render_task_row(store, meta):
    key = meta["key"]
    b = S.get_task_binding(store, key)
    opts = _task_model_options(store, meta["cats"])
    ids = [o[0] for o in opts]
    labels = {o[0]: o[1] for o in opts}
    lock = "🔒 " if meta.get("free_only") else ""
    with st.expander(f"{lock}{meta['label']}  ·  {'on' if b['enabled'] else 'off'}",
                     expanded=False):
        if meta.get("fusion"):
            st.caption("🔗 Fusion task — the **secondary runs alongside the primary** and "
                       "their scores are combined into one ranking (not a failover). "
                       "Fallback is only used if a contributor errors.")
        if meta.get("batched"):
            st.caption("📦 Batched — one call covers all segments (never per-segment).")
        # For a fusion task the middle slot is a parallel contributor, not a fallback.
        slot_labels = {"primary": "Primary", "fallback": "Fallback",
                       "secondary": "Secondary (fusion contributor)" if meta.get("fusion")
                       else "Secondary"}
        c = st.columns(3)
        sel = {}
        for i, slot in enumerate(("primary", "secondary", "fallback")):
            cur = b[slot] if b[slot] in ids else ""
            sel[slot] = c[i].selectbox(
                slot_labels[slot], ids, index=ids.index(cur),
                format_func=lambda x: labels.get(x, x), key=f"tk_{key}_{slot}")
        t = st.columns(2)
        enabled = t[0].checkbox("Enabled", b["enabled"], key=f"tk_{key}_en")
        if meta.get("free_only"):
            t[1].caption("🔒 FREE enforced — paid providers are refused for this task.")
            paid = False
        else:
            paid = t[1].checkbox("Allow paid providers", b["paid_allowed"],
                                 key=f"tk_{key}_paid")
        if st.button("Save binding", key=f"tk_{key}_save"):
            S.set_task_binding(store, key, primary=sel["primary"], secondary=sel["secondary"],
                               fallback=sel["fallback"], enabled=enabled, paid_allowed=paid)
            _persist(store)
            st.success("Saved.")
            st.rerun()

        if meta.get("fusion"):
            fus = S.resolve_fusion(store, key)
            _name = lambda m: (m.get("display_name") or m.get("model") or m.get("name"))
            st.caption("Fuses: " + (" + ".join(_name(m) for m in fus["contributors"]) or "(none)")
                       + (f"  ·  fallback {', '.join(_name(m) for m in fus['fallback'])}"
                          if fus["fallback"] else ""))
        else:
            chain = S.resolve_task(store, key)
            if chain:
                st.caption("Resolves to: " + " → ".join(
                    (m.get("display_name") or m.get("model") or m.get("name")) for m in chain))
            else:
                st.caption("Resolves to: (nothing available)")
        viol = S.task_paid_violation(store, key)
        if viol:
            st.error(viol)


def _render_model_row(store, cred, m):
    mid = m["id"]
    caps = m.get("capabilities", {}) or {}
    summary = _caps_summary({"category": m.get("category"), "model": m.get("model"),
                             "capabilities": caps})
    label = m.get("display_name") or ""
    cols = st.columns([4, 2, 2, 1, 1, 1, 1])
    # BUG2: show the EXACT model id that will be sent, verbatim.
    cols[0].markdown(
        (f"**{label}**  \n" if label else "")
        + f"sends model id: `{m.get('model', '')}`  \n<small>{summary}</small>",
        unsafe_allow_html=True)
    enabled = cols[1].checkbox(S.category_label(m.get("category", "")),
                               m.get("enabled", True), key=f"me_{mid}")
    if enabled != m.get("enabled", True):
        S.update_model(store, mid, enabled=enabled)
        _persist(store)
    # R2 cost tier (free/paid/unknown) — free is required for frame-scoring/OCR.
    cur_tier = m.get("tier", "unknown")
    tier = cols[2].selectbox("tier", S.TIERS, index=S.TIERS.index(cur_tier)
                             if cur_tier in S.TIERS else 0, key=f"mt2_{mid}",
                             label_visibility="collapsed")
    if tier != cur_tier:
        S.update_model(store, mid, tier=tier)
        _persist(store)
    if cols[2].button("🔍", key=f"mtd_{mid}", help="Test & Detect this model"):
        # Persist first so the probe tests EXACTLY what the CLI/production will load
        # from disk — no session-vs-disk key drift (the Forge 401 class of bug).
        _persist(store)
        cur = S.get_credential(store, cred["id"])
        with st.spinner("Probing…"):
            res = D.detect(m.get("category"), cur.get("base_url", ""),
                           cur.get("api_key", ""), m.get("model", ""),
                           auth_style=cur.get("auth_style", "bearer"),
                           auth_header_name=cur.get("auth_header_name"))
        S.update_model(store, mid, capabilities=res, api_shape=res.get("api_shape"),
                       voices=res.get("voices", []))
        _persist(store)
        st.rerun()
    if cols[3].button("⬆", key=f"mup_{mid}", help="Higher priority"):
        S.move_model_priority(store, m["category"], mid, -1)
        _persist(store)
        st.rerun()
    if cols[4].button("⬇", key=f"mdn_{mid}", help="Lower priority"):
        S.move_model_priority(store, m["category"], mid, +1)
        _persist(store)
        st.rerun()
    if cols[5].button("🗑", key=f"mdel_{mid}", help="Remove model"):
        S.delete_model(store, mid)
        _persist(store)
        st.rerun()

    # GENERAL: every probe shows status + full URL + exact payload + response body.
    for note in caps.get("notes", []):
        st.caption("• " + note)
    if caps.get("request_excerpt") or caps.get("response_excerpt"):
        with st.expander(f"Raw probe (HTTP {caps.get('status_code', '—')}) — request / response"):
            if caps.get("request_excerpt"):
                st.code(caps["request_excerpt"])
            if caps.get("response_excerpt"):
                st.code(caps["response_excerpt"])
