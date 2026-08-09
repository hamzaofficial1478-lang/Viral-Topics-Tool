"""Settings screen — visual API-provider manager (no .env editing needed).

Add unlimited providers, auto-detect their capabilities ("Test & Detect"), run a
real sample call ("Test", with an audio player for TTS), order them for failover,
enable/disable, and see everything in one overview table. Everything is saved to
the gitignored provider store; keys are masked after saving and never logged.
"""

from __future__ import annotations

import json
import os

import streamlit as st

from ..providers import store as S
from ..providers import detect as D
from ..providers import capability_warnings

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


def _render_notifications(store: dict) -> None:
    """ntfy.sh progress messages + remote control — the only channel (Telegram
    was removed: ntfy needs no bot/account setup and has a free phone app, so
    running two channels bought nothing)."""
    st.divider()
    st.subheader("📨 Notifications & remote control (ntfy)")
    st.caption("Get a message when the UI opens, when each link finishes, when the whole "
               "queue is done, and when it stops — and control it back from your phone. "
               "No account, no token required.")
    with st.expander("How to set up ntfy (2 minutes)", expanded=False):
        st.markdown(
            "1. Install the free **ntfy** app (Android/iOS) — or just open ntfy.sh in a browser.\n"
            "2. Invent a **topic name** nobody else would guess, e.g. "
            "`shortforge-ammar-7f3k9`. Anyone who knows the topic can read your "
            "messages, so make it long and random.\n"
            "3. In the app: **Subscribe to topic** → type the same name.\n"
            "4. Paste it below → Save → Test.")
    from ..notify import test_ntfy
    nt = store.get("ntfy", {}) or {}
    topic = st.text_input("ntfy topic", value=nt.get("topic", "") or "",
                          placeholder="shortforge-yourname-7f3k9")
    server = st.text_input("ntfy server", value=nt.get("server", "") or "https://ntfy.sh")
    st.caption("**Send commands from your phone too.** Use a DIFFERENT, longer topic for "
               "commands — anyone who knows a topic name can publish to it, and publishing "
               "is how commands arrive. This is also what `cli.py listen` / `start_all.bat` "
               "listens on to open the UI, ask permission before starting, and take links.")
    cmd_topic = st.text_input("ntfy command topic",
                              value=nt.get("command_topic", "") or "",
                              placeholder="shortforge-cmd-9x2v7q4b1m")
    token_v = st.text_input("ntfy access token (optional, recommended)",
                            value=nt.get("token", "") or "", type="password",
                            help="From a paid/self-hosted ntfy account. With a token the "
                                 "topic is private; without one, treat the name as the secret.")

    if st.button("🩺 Diagnose connection (run this if Test times out)", width="stretch"):
        from ..netdiag import report
        host = None
        try:
            import urllib.parse as _up
            host = _up.urlparse(server.strip() or "https://ntfy.sh").hostname
        except Exception:  # noqa: BLE001
            pass
        with st.spinner(f"Checking DNS → TCP → TLS → HTTPS to {host or 'ntfy.sh'}…"):
            st.code(report(host=host, timeout=10))

    n1, n2 = st.columns(2)
    if n1.button("💾 Save ntfy", width="stretch"):
        store["ntfy"] = {"topic": topic.strip() or None, "server": server.strip() or None,
                         "command_topic": cmd_topic.strip() or None,
                         "token": token_v.strip() or None}
        _persist(store)
        st.success("Saved.")
    if n2.button("🔔 Test ntfy", width="stretch", disabled=not topic.strip()):
        ok, detail = test_ntfy(topic.strip(), server.strip() or "https://ntfy.sh")
        (st.success if ok else st.error)(detail)

    _render_notification_reference()


def _render_notification_reference() -> None:
    """Exactly which messages arrive, and what to send back. Both were asked for
    directly: 'which notifications i will get on ntfy?' and 'make it so i can add
    links with details from ntfy'."""
    from ..config import Config
    from ..lifecycle import read_state

    with st.expander("What you'll be messaged, and what you can send back", expanded=False):
        st.markdown(
            "**You get a message when:**\n"
            "- 🖥️ the UI opens (says what's queued, and asks permission before starting — "
            "see below)\n"
            "- ⚡ it restarts after a power cut (names the link it was interrupted on, how "
            "many clips it had already made for that link, and how many overall)\n"
            "- 🎬 each link **starts** — with the numbers it understood "
            "(*“making 6 clip(s) of 1m00s, 16:9”*)\n"
            "- ✅ each link **finishes** — clip count and how long it took\n"
            "- ⚠️ a link **fails** — plain-language cause, and whether it self-healed\n"
            "- 🌐 the **connection drops** and again when it comes back (best-effort: an "
            "alert genuinely can't be pushed through a channel that's down, but recovery is "
            "always reported, and the dashboard below shows it live either way)\n"
            "- 🏁 the whole batch is **done**\n"
            "- 🔴 ShortForge **stops** — Ctrl+C, closing the window, or PC shutdown — and "
            "names what it was working on and how many clips it had made\n\n"
            "**On every start, permission is asked before anything runs.** ShortForge never "
            "auto-resumes — reply **“start”** (or `/resume`) to begin, or **“pause”** (or "
            "`/stop`) to leave it stopped. Same wording either way — no slash needed.\n\n"
            "**You can send** (to the *command* topic):\n"
            "```\n"
            "https://youtu.be/AAA 5 clips of 2 min landscape\n"
            "https://youtu.be/BBB clips=6 duration=90s aspect=9:16 label=podcast\n"
            "```\n"
            "Settings apply to every link in that message — send one message per "
            "group. Duration accepts `120`, `2min`, `90s` or `1:30`.\n\n"
            "`/status` · `/list` · `/pause` · `/resume` · `/clear` · `/cancel` · `/help`")

    work_dir = Config.load().get("paths.work_dir", ".shortforge")
    state = read_state(work_dir)
    if state:
        import time as _t
        ago = int(max(0, _t.time() - float(state.get("last_seen") or 0)))
        cur = (state.get("current") or {}).get("url", "")
        if ago < 120:
            st.success(f"🟢 A worker is running right now ({state.get('mode', '?')} mode)"
                       + (f" — on {cur[:60]}" if cur else ""))
        else:
            # Not "offline": this file is exactly what a power cut leaves behind.
            st.warning(f"⚠️ A worker registered {ago // 60} min ago and hasn't checked in. "
                       f"If you didn't close it, it was killed — starting it again resumes "
                       f"the queue.")
        if state.get("ntfy_ok") is False:
            since = state.get("ntfy_down_since")
            since_txt = ""
            if since:
                mins = int(max(0, _t.time() - float(since)) // 60)
                since_txt = f" (about {mins} min)" if mins else " (just now)"
            st.error(f"🔌 ntfy connection lost{since_txt} — commands and push notifications "
                    f"won't arrive until it reconnects. Rendering is unaffected; the queue "
                    f"keeps its place. {state.get('ntfy_detail', '')}".strip())
    else:
        st.info("⚪ No worker is running. Start one with `start_all.bat` "
                "(or `python cli.py listen --owner-confirmed`).")


def _render_youtube_auth(store: dict) -> None:
    """YouTube auth: browser/cookies picker + Test + Update yt-dlp. Persisted to
    the store and applied to every download."""
    from ..config import Config
    from ..ingest import list_formats, test_youtube_auth, update_ytdlp, ytdlp_version

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
    concurrent_fragments = st.number_input(
        "Concurrent fragment downloads", min_value=1, max_value=16, step=1,
        value=int(ya.get("concurrent_fragments") or 4),
        help="How many pieces of the video yt-dlp fetches at once. Above ~360p, YouTube "
             "always serves video in separate fragments — yt-dlp's own default is 1 (fully "
             "one-at-a-time), which leaves a fast connection mostly idle. Raising this is "
             "usually the biggest lever for a slow download; going far above ~8-10 rarely "
             "helps further and can look like abuse to YouTube's servers.")
    test_url = st.text_input("Test URL (paste one of your video links)", key="yt_test_url",
                             placeholder="https://www.youtube.com/watch?v=…")

    c1, c2 = st.columns(2)
    if c1.button("💾 Save authentication", width="stretch"):
        store["youtube_auth"] = {
            "cookies_from_browser": None if browser == "none" else browser,
            "cookies_file": cookies_file.strip() or None,
            "concurrent_fragments": int(concurrent_fragments),
        }
        _persist(store)
        st.success("Saved — applied to every download.")
    if c2.button("🔎 Test authentication", width="stretch", disabled=not test_url.strip()):
        cfg = Config.load()
        cfg.set("ingest.cookies_from_browser", None if browser == "none" else browser)
        cfg.set("ingest.cookies", cookies_file.strip() or None)
        with st.spinner("Fetching metadata (no download) — trying every strategy…"):
            ok, detail = test_youtube_auth(test_url.strip(), cfg)
        # The Test walks the same format fallback chain the download does, so a ✓
        # here means the real download works — not merely that the page loaded.
        (st.success if ok else st.error)(
            "This link will download ✓" if ok else "No strategy worked ✗")
        st.code(detail or "(no result)")   # per-strategy: confirms whether your browser cookies work

    if st.button("🧾 List available formats (diagnostic)", disabled=not test_url.strip()):
        cfg = Config.load()
        cfg.set("ingest.cookies_from_browser", None if browser == "none" else browser)
        cfg.set("ingest.cookies", cookies_file.strip() or None)
        with st.spinner("Asking YouTube what it will serve…"):
            ok, detail = list_formats(test_url.strip(), cfg)
        st.code(detail or "(no result)")
        if not ok:
            st.warning("Update yt-dlp below, then test again.")

    ver = ytdlp_version() or "not installed"
    st.caption(f"yt-dlp version: **{ver}** — extractors break often; keep it current.")
    if st.button("⬆️ Update yt-dlp"):
        with st.spinner("Running pip install -U yt-dlp…"):
            ok, detail = update_ytdlp()
        (st.success if ok else st.error)(detail)


def _render_disk_usage() -> None:
    """What the working directory is holding, and a safe way to reclaim it.

    Cached source videos are gigabytes and nothing ever removed them: the only
    cleanup in the codebase fired *after* a render had already died on "no
    space left on device". On a modest disk that is a scheduled failure, and
    the operator cannot hand-edit files or run cleanup scripts — so it has to
    be visible and one click away here.
    """
    from ..config import Config
    from .. import maintenance as M

    st.divider()
    st.subheader("🧹 Disk space")
    work_dir = Config.load().get("paths.work_dir", ".shortforge")
    r = M.cache_report(work_dir)

    c1, c2, c3 = st.columns(3)
    c1.metric("Reclaimable", M.human_gb(r["reclaimable_bytes"]),
              help="Downloaded source videos plus the per-video working folders "
                   "(large WAV intermediates used for transcription and audio). "
                   "All re-derivable — deleting costs time to redo, nothing else.")
    c2.metric("Working folder", M.human_gb(r["total_bytes"]))
    c3.metric("Free on disk", M.human_gb(r["free_bytes"]))
    if r["free_bytes"] is not None and r["free_bytes"] < 20 * 1024 ** 3:
        st.warning("⚠️ Under 20 GB free — downloads, stems and renders all need room. "
                   "Clearing old downloads below is the safest space to reclaim.")
    st.caption(f"Per-video working folders: {M.human_gb(r['source_cache_bytes'])} "
               f"({r['source_cache_dirs']}) — extracted audio kept for re-runs; a 29-min "
               f"video leaves ~400 MB here. This is usually what fills the folder.\n\n"
               f"Hook scores held: {M.human_gb(r['hookscores_bytes'])} "
               f"({r['hookscores_files']} file(s)) — these are kept deliberately. Each one "
               "is a **paid** LLM scoring pass, and they're tiny, so deleting them would "
               "cost money to rebuild and free almost nothing.")

    keep_h = st.number_input("Remove cached downloads older than (hours)", min_value=1,
                             max_value=24 * 90, value=48, step=24,
                             help="Anything the running job needs is minutes old, so it can "
                                  "never be caught by this.")
    n_old, freed = M.prune_downloads(work_dir, keep_hours=float(keep_h), dry_run=True)
    n_src, freed_src = M.prune_source_caches(work_dir, keep_hours=float(keep_h), dry_run=True)
    n_old += n_src
    freed += freed_src
    d1, d2 = st.columns(2)
    if d1.button(f"🧽 Remove {n_old} old download(s) · {M.human_gb(freed)}",
                 width="stretch", disabled=not n_old):
        n, got = M.prune_downloads(work_dir, keep_hours=float(keep_h))
        n2, got2 = M.prune_source_caches(work_dir, keep_hours=float(keep_h))
        st.success(f"Removed {n + n2} item(s), freed {M.human_gb(got + got2)}.")
        st.rerun()
    if d2.button("🗑 Remove ALL cached downloads", width="stretch",
                 disabled=not r["downloads_files"]):
        got = M.clear_downloads(work_dir)
        st.success(f"Cleared every cached download — freed {M.human_gb(got)}. "
                   "Sources re-download on demand.")
        st.rerun()
    st.caption("Your job queue, worker state and chat history are never touched by either "
               "button — only re-downloadable cache.")


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

    # --- ntfy notifications + remote control ---
    _render_notifications(store)

    # --- YouTube authentication (fix the bot wall on downloads) ---
    _render_youtube_auth(store)

    # --- disk usage + safe reclaim (downloads grow forever otherwise) ---
    _render_disk_usage()

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
