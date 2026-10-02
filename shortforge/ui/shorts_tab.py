"""The 📥 YT Shorts screen — a second tool beside the long-video clip maker.

Layout follows the operator's TikTok downloader (saved accounts, each with its
own count and on/off switch; a paste box for one-off links; a live download
list; a history), rebuilt in this app's own Streamlit style.

Nothing on this screen is kept only in the browser. Every change is written to
``shorts_data/`` the moment it is made — a channel's count as soon as the number
changes, a switch as soon as it is flipped — so reopening the tab, restarting
the program or rebooting always shows exactly what was saved. Downloads run in
a separate process that keeps going with the tab (or the whole window) closed.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
import time

import streamlit as st

from ..config import Config
from ..shorts import store as S
from ..shorts import worker as W
from ..shorts import youtube as YT
from .lazy import lazy_tabs

_STATUS_ICON = {S.PENDING: "⏳", S.DOWNLOADING: "⏬", S.DONE: "✅", S.SKIPPED: "⏭",
                S.FAILED: "✗", S.CANCELLED: "🚫"}
_QUALITY = {"best": "Best available — no limit (recommended)", "2160": "Up to 2160p (4K)",
            "1440": "Up to 1440p", "1080": "Up to 1080p", "720": "Up to 720p"}
_CODEC = {"best": "Best picture: biggest size, then the most detail (highest bitrate), any codec",
          "compatible": "Same size, but prefer H.264 when there's a choice (older devices)"}


# --- helpers ----------------------------------------------------------------- #

def _open_folder(path: str) -> tuple[bool, str]:
    try:
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _flash(kind: str, text: str) -> None:
    """Queue a message to show after the rerun that follows an action.

    Without this, adding three good links and one bad one showed the "not a
    YouTube link" warning for a fraction of a second: the rerun that refreshes
    the list wiped it before it could be read.
    """
    st.session_state.setdefault("sh_flash", []).append((kind, text))


def _show_flash() -> None:
    for kind, text in st.session_state.pop("sh_flash", []):
        getattr(st, kind, st.info)(text)


def _mb(n) -> str:
    n = float(n or 0)
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def ensure_worker(dd: str) -> str:
    """Make sure something is downloading; return what to tell the operator.

    If a downloader already holds the Shorts lock, it picks the new work up.
    Otherwise start `cli.py shorts run` as its own process — on Windows detached
    from this window, so closing the dashboard does not stop it.

    This deliberately does NOT defer to a running `cli.py listen` just because
    one is alive: a listener started before an update has no Shorts thread and
    would never pick the work up. Starting one here is always safe — whichever
    process takes the lock first does the work, and the other stands aside, so
    there are never two downloaders.
    """
    if S.lock_is_live(dd):
        return "Added — the download already running will get to it."
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    kwargs: dict = {"cwd": here, "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.join(here, "cli.py"), "shorts", "run"], **kwargs)
    return "Started. It keeps going even if you close this tab or the dashboard window."


# --- live status (auto-refreshing, shown above every tab) --------------------- #

def _status_panel(dd: str) -> None:
    run = S.load_run(dd)
    c = S.counts(run)
    live = S.lock_is_live(dd)
    to_list = len(run.get("channels_to_list", []))
    left = c[S.PENDING] + c[S.DOWNLOADING] + to_list
    paused = S.is_paused(run) and S.has_work(run)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Downloaded this run", c[S.DONE])
    m2.metric("Still to go", left if not to_list else f"{left}+")
    m3.metric("Failed", c[S.FAILED])
    m4.metric("All-time downloads", len(S.history_items(dd)))

    if live and not paused:
        p = W.read_progress(dd)
        if p.get("phase") == "listing":
            st.info(f"🔎 Finding new Shorts on **{p.get('channel', '')}**…")
        elif p.get("id"):
            frac = p.get("fraction")
            label = (f"⏬ {p.get('channel') or ''} — {(p.get('title') or p['id'])[:70]}")
            if frac is not None:
                speed = p.get("speed")
                extra = f" · {speed / 1e6:.1f} MB/s" if speed else ""
                st.progress(min(1.0, max(0.0, float(frac))), text=f"{label} · {frac:.0%}{extra}")
            else:
                st.info(label)
        else:
            st.info("⏬ Downloading…")
    elif paused:
        st.warning(f"⏸ **Paused** — {left} step(s) left. Nothing is lost; press Resume "
                   "to carry on.")
    elif S.has_work(run):
        st.info("⏳ Queued — waiting for the downloader to start.")
    else:
        st.success("✅ Nothing queued. Pick channels below, or paste links.")

    b1, b2, b3 = st.columns(3)
    if paused or (S.has_work(run) and not live):
        if b1.button("▶️ Resume", type="primary", width="stretch", key="sh_resume"):
            S.set_paused(dd, False)
            st.toast(ensure_worker(dd))
            st.rerun()
    elif live:
        if b1.button("⏸ Stop after this one", width="stretch", key="sh_pause",
                     help="Finishes the Short downloading now, then stops. Nothing is lost."):
            S.set_paused(dd, True)
            st.toast("Stopping after the current Short.")
            st.rerun()
    if c[S.FAILED] and b2.button(f"🔁 Retry {c[S.FAILED]} failed", width="stretch",
                                 key="sh_retry"):
        S.retry_failed(dd)
        S.set_paused(dd, False)
        st.toast(ensure_worker(dd))
        st.rerun()
    if S.has_work(run) and b3.button("🛑 Cancel what's left", width="stretch",
                                     key="sh_cancel",
                                     help="Drops the queued Shorts. Downloaded ones stay."):
        S.cancel_pending(dd)
        st.rerun()


# --- tabs -------------------------------------------------------------------- #

def _tab_channels(dd: str) -> None:
    st_ = S.settings(dd)
    data = S.load_channels(dd)
    chans = data["channels"]
    taken = S.taken_by_channel(dd)

    with st.expander("➕ Add channels", expanded=not chans):
        text = st.text_area(
            "Channels — one per line, paste as many as you like", key="sh_add_text",
            height=110,
            placeholder="@MrBeast\nhttps://www.youtube.com/@veritasium\n"
                        "https://www.youtube.com/channel/UCBR8-60-B28hp2BmDPdntcQ")
        a1, a2 = st.columns([1, 2])
        count = a1.number_input("Shorts per run (each)", min_value=1, max_value=500,
                                value=int(st_["default_count"]), key="sh_add_count")
        rights = a2.checkbox("These are my channels, or I have the rights to reuse their "
                             "Shorts", key="sh_add_rights")
        lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        if st.button(f"➕ Add {len(lines) or ''} channel(s)".replace("  ", " "),
                     type="primary", disabled=not (lines and rights), key="sh_add_btn"):
            added, problems = S.add_channels(dd, text, int(count), rights_confirmed=rights)
            if added:
                _flash("success", f"Saved {len(added)} channel(s). They stay saved until "
                                  f"you remove them.")
            for p in problems:
                _flash("warning", p)
            if added:
                st.session_state.pop("sh_add_text", None)
                _reset_table(S.MAIN)
            st.rerun()

    if not chans:
        st.info("No channels saved yet. Add some above — or find some in "
                "🔎 Find channels. They're kept on disk, so they are still here next "
                "time you open the program.")
        return

    mine = [c for c in chans if S.channel_list(c) == S.MAIN]
    wish = [c for c in chans if S.channel_list(c) == S.WISHLIST]
    _channel_section(dd, mine, taken, S.MAIN, "📺 Your channels")
    if wish:
        st.write("")
        _channel_section(dd, wish, taken, S.WISHLIST, f"⭐ Wishlist ({len(wish)})",
                         caption="Channels you added from 🔎 Find channels. A download only "
                                 "includes them when the box below is ticked.")

    st.write("")
    inc = st.checkbox(
        f"Also download from the wishlist ({sum(1 for c in wish if c.get('enabled', True))} "
        f"switched on)", value=bool(st_.get("include_wishlist")), key="sh_inc_wish",
        disabled=not wish, on_change=_on_include_wishlist, args=(dd,),
        help="Remembered. Applies to this button and to “shorts start” from your phone.")
    keys = S.download_keys(dd, include_wishlist=inc)
    picked = [c for c in chans if c["key"] in keys]
    total = sum(int(c.get("count", 0)) for c in picked)
    if st.button(f"⏬ Download {total} new Short(s) from {len(picked)} channel(s)",
                 type="primary", width="stretch", disabled=not picked, key="sh_go"):
        q = S.start_run(dd, keys, [])
        _flash("success", f"Queued {q['channels']} channel(s). " + ensure_worker(dd))
        st.rerun()


def _on_include_wishlist(dd: str) -> None:
    S.update_settings(dd, include_wishlist=bool(st.session_state["sh_inc_wish"]))


def _channel_section(dd: str, chans: list[dict], taken: dict, which: str, title: str,
                     caption: str = "") -> None:
    """One list of channels as ONE table — switch and count editable in place.

    It used to be a bordered row per channel with its own switch, number box
    and buttons: with a few dozen channels that is hundreds of widgets for the
    browser to draw, and opening this tab took over a second. A table is one.
    """
    if not chans:
        return
    enabled = [c for c in chans if c.get("enabled", True)]
    h1, h2, h3 = st.columns([3, 1, 1])
    h1.markdown(f"**{title}** — {len(enabled)} of {len(chans)} switched on")
    if caption:
        h1.caption(caption)
    if h2.button("Turn all on", width="stretch", disabled=len(enabled) == len(chans),
                 key=f"sh_allon_{which}"):
        S.set_all_enabled(dd, True, which)
        _reset_table(which)
        st.rerun()
    if h3.button("Turn all off", width="stretch", disabled=not enabled,
                 key=f"sh_alloff_{which}"):
        S.set_all_enabled(dd, False, which)
        _reset_table(which)
        st.rerun()

    wish = which == S.WISHLIST
    rows = []
    for c in chans:
        row = {"On": bool(c.get("enabled", True)),
               "Channel": c.get("name") or c["key"],
               "Shorts per run": int(c.get("count", 10)),
               "Downloaded so far": taken.get(c["key"], 0)}
        if wish:
            row.update({"Subscribers": _short_count(c["subscribers"]) if c.get("subscribers")
                        else "", "Country": c.get("country") or "",
                        "Language": c.get("language") or ""})
        row["Link"] = f"{c['url']}/shorts"
        rows.append(row)
    tkey = _table_key(which)
    st.data_editor(
        rows, key=tkey, hide_index=True, width="stretch", num_rows="fixed",
        height=min(560, 38 + 35 * len(rows)),
        disabled=[k for k in rows[0] if k not in ("On", "Shorts per run")],
        column_config={
            "On": st.column_config.CheckboxColumn(
                "On", width="small", help="Switched off = skipped, but its count is kept."),
            "Shorts per run": st.column_config.NumberColumn(
                "Shorts per run", min_value=1, max_value=500, step=1, format="%d",
                help="How many NEW Shorts to take from this channel each time you "
                     "download. Saved the moment you change it."),
            "Link": st.column_config.LinkColumn("Link", display_text="open"),
        },
        on_change=_on_table_edit, args=(dd, [c["key"] for c in chans], tkey))
    st.caption("Tick **On** or change **Shorts per run** right in the table — saved "
               "the moment you change it.")

    names = {f"{c.get('name') or c['key']} ({c['key']})": c["key"] for c in chans}
    a1, a2 = st.columns([3, 2])
    picked = a1.multiselect("Remove" + (" or move" if wish else "") + " channels",
                            list(names), key=f"sh_pick_{which}",
                            placeholder="Pick channels…", label_visibility="collapsed")
    keys = [names[n] for n in picked]
    confirm = f"sh_rm_confirm_{which}"
    with a2:
        b1, b2 = st.columns(2)
        if st.session_state.get(confirm) and keys:
            if b1.button(f"Sure? Remove {len(keys)}", type="primary", width="stretch",
                         key=f"sh_rm_yes_{which}",
                         help="Removes the channels. Their downloads and history stay."):
                for k in keys:
                    S.remove_channel(dd, k)
                _after_list_change(which, confirm)
                st.rerun()
        elif b1.button("🗑 Remove", width="stretch", disabled=not keys,
                       key=f"sh_rm_{which}", help="Asks once more before removing."):
            st.session_state[confirm] = True
            st.rerun()
        if wish and b2.button("➕ To your channels", width="stretch", disabled=not keys,
                              key=f"sh_promote_{which}",
                              help="Downloaded every time, not only with the box ticked."):
            for k in keys:
                S.update_channel(dd, k, list=S.MAIN)
            _flash("success", f"Moved {len(keys)} channel(s) to your channels.")
            _after_list_change(which, confirm)
            st.rerun()


def _table_key(which: str) -> str:
    return f"sh_tbl_{which}_{st.session_state.get(f'sh_tbl_ver_{which}', 0)}"


def _reset_table(which: str) -> None:
    """Start the table afresh after its rows changed outside it (removed, moved,
    all switched) — its pending edits refer to rows by position."""
    st.session_state[f"sh_tbl_ver_{which}"] = st.session_state.get(f"sh_tbl_ver_{which}", 0) + 1


def _after_list_change(which: str, confirm: str) -> None:
    st.session_state.pop(confirm, None)
    st.session_state.pop(f"sh_pick_{which}", None)
    for w in (S.MAIN, S.WISHLIST):          # a move changes both lists
        _reset_table(w)


def apply_channel_edits(dd: str, keys: list[str], edited_rows: dict) -> int:
    """Save what was changed in a channel table: ``{row: {column: value}}``.
    Returns how many channels changed."""
    n = 0
    for row, changes in (edited_rows or {}).items():
        try:
            key = keys[int(row)]
        except (IndexError, ValueError):
            continue
        fields = {}
        if "On" in changes:
            fields["enabled"] = bool(changes["On"])
        if changes.get("Shorts per run") is not None:
            fields["count"] = max(1, min(500, int(changes["Shorts per run"])))
        if fields and S.update_channel(dd, key, **fields) is not None:
            n += 1
    return n


def _on_table_edit(dd: str, keys: list[str], tkey: str) -> None:
    apply_channel_edits(dd, keys, (st.session_state.get(tkey) or {}).get("edited_rows"))


def _short_count(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suf
    return str(n)


# --- 🔎 Find channels ------------------------------------------------------ #

def spawn_search(dd: str, job_id: str) -> None:
    """Start the search in its own process (patched out in tests)."""
    from ..shorts import discover as DS
    DS.spawn(dd, job_id)


def _tab_find(dd: str, run_every: str) -> None:
    from ..shorts import countries as CO
    from ..shorts import discover as DS
    from ..shorts import langid as LG

    st.caption("Find Shorts channels by describing them, or ask for more channels like "
               "one you give. Every result is checked first: it really posts Shorts, its "
               "language is read from its titles, and its country from its own About page.")
    mode = st.radio("How do you want to search?", ["describe", "like"], horizontal=True,
                    key="dc_mode",
                    format_func=lambda m: {"describe": "✍️ Describe it in words",
                                           "like": "🔗 Like a channel I give"}[m])
    ref = ""
    if mode == "like":
        ref = st.text_input("Reference channel", key="dc_ref",
                            placeholder="@handle or https://www.youtube.com/@channel")
        prompt = st.text_input("Anything to add? (optional)", key="dc_extra",
                               placeholder="e.g. only street food, no restaurants")
    else:
        prompt = st.text_area("What kind of Shorts channels are you looking for?",
                              key="dc_prompt", height=90,
                              placeholder="e.g. Pakistani home cooking, quick recipes, "
                                          "spoken in Urdu")
    same = [("same", "Same as the reference")] if mode == "like" else []
    c_opts = same + CO.choices()
    l_opts = same + LG.LANGUAGES
    f1, f2, f3 = st.columns([1, 1.4, 1.4])
    count = f1.number_input("How many channels", 1, 50, 10, key="dc_count")
    country = f2.selectbox("Country", [c for c, _ in c_opts], key="dc_country",
                           format_func=lambda c: dict(c_opts)[c])
    language = f3.selectbox("Language of their Shorts", [c for c, _ in l_opts],
                            key="dc_lang", format_func=lambda c: dict(l_opts)[c],
                            help="Urdu and Hindi also match Shorts written in Roman letters "
                                 "(“recipe kaise banaye”) — the two can't be told apart "
                                 "in that script.")
    ai_ok = DS.ai_available()
    with st.expander("More filters"):
        a1, a2 = st.columns(2)
        min_subs = a1.number_input("Minimum subscribers", 0, 100_000_000, 0, step=1000,
                                   key="dc_minsubs")
        min_shorts = a2.number_input("Minimum Shorts on the channel", 1, 15, 3,
                                     key="dc_minshorts",
                                     help="Out of the 15 newest it looks at — filters out "
                                          "channels that rarely post Shorts.")
        unlisted = st.toggle("Include channels that don't show their country", value=True,
                             key="dc_unlisted",
                             help="Many channels never set a country on YouTube. When on, "
                                  "they can fill up the results AFTER the confirmed ones, "
                                  "marked “not listed”.")
        use_ai = st.toggle("Use AI to understand the description and check each channel fits",
                           value=ai_ok, key="dc_ai", disabled=not ai_ok,
                           help=("Uses the model bound to “Shorts channel finder” in Task "
                                 "routing." if ai_ok else
                                 "No AI model is set up — searching by keywords."))

    jobs = DS.list_jobs(dd)
    running = next((j for j in jobs if DS.is_alive(j)), None)
    ready = (bool(prompt.strip()) if mode == "describe" else bool(ref.strip()))
    b1, b2 = st.columns([3, 1])
    if b1.button("🔎 Find channels", type="primary", width="stretch", key="dc_go",
                 disabled=not ready or running is not None):
        try:
            job = DS.new_job(dd, {"mode": mode, "prompt": prompt, "reference": ref,
                                  "count": int(count), "country": country,
                                  "language": language, "min_subscribers": int(min_subs),
                                  "min_shorts": int(min_shorts), "use_ai": bool(use_ai),
                                  "allow_unlisted_country": bool(unlisted)})
        except Exception as e:  # noqa: BLE001 - show the reason
            st.error(str(e))
        else:
            spawn_search(dd, job["id"])
            st.session_state["dc_view"] = job["id"]
            st.rerun()
    if running and b2.button("⏹ Stop", width="stretch", key="dc_stop"):
        DS.cancel(dd, running["id"])
        st.rerun()

    if not jobs:
        return
    st.divider()
    labels = {j["id"]: _job_label(j) for j in jobs}
    current = st.session_state.get("dc_view")
    if current not in labels:
        current = jobs[0]["id"]
    view = st.selectbox("Search", list(labels), index=list(labels).index(current),
                        format_func=lambda i: labels[i], key="dc_view")
    job = DS.load_job(dd, view)
    if job and DS.is_alive(job):
        st.fragment(run_every=run_every)(_search_live)(dd, view)
    elif job:
        _search_results(dd, job)


def _job_label(j: dict) -> str:
    p = j.get("params", {})
    what = (f"like {p.get('reference', '')[:40]}" if p.get("mode") == "like"
            else f"“{p.get('prompt', '')[:45]}”")
    n = len(j.get("results") or [])
    state = {"running": "searching…", "queued": "starting…", "done": f"{n} found",
             "failed": "failed", "cancelled": f"stopped · {n} found"}.get(j.get("status"), "")
    when = time.strftime("%d %b %H:%M", time.localtime(j.get("created") or 0))
    return f"{when} — {what} — {state}"


def _search_live(dd: str, job_id: str) -> None:
    from ..shorts import discover as DS
    job = DS.load_job(dd, job_id)
    if not job:
        return
    if not DS.is_alive(job):
        st.rerun(scope="app")            # finished: switch to the interactive results
    prog = job.get("progress") or {}
    want = (job.get("params") or {}).get("count") or 1
    got = len(job.get("results") or [])
    st.progress(min(1.0, got / want),
                text=f"🔎 {job.get('stage', '')} — {got} of {want} found"
                     + (f" · {prog.get('checked', 0)} checked" if prog.get("checked") else ""))
    _results_table(job, readonly=True)


def _search_results(dd: str, job: dict) -> None:
    from ..shorts import discover as DS
    eff = job.get("effective") or {}
    status = job.get("status")
    if status == "failed":
        st.error(f"The search failed: {job.get('error')}")
    elif job.get("status") in ("running", "queued"):
        st.warning("This search stopped without finishing (the program was closed?). "
                   "Run it again to continue.")
    want = (job.get("params") or {}).get("count") or 0
    results = job.get("results") or []
    head = f"**{len(results)} of {want} found**"
    if eff:
        head += f" · country: {eff.get('country_name')} · language: {eff.get('language_name')}"
    st.markdown(head)
    if job.get("reference"):
        r = job["reference"]
        st.caption(f"Reference: {r.get('name')} — {_lang_label(r.get('language'))}, "
                   f"{r.get('country_code') or 'country not listed'}")
    for n in job.get("notes") or []:
        st.info(n)
    if job.get("queries"):
        st.caption("Searched for: " + " · ".join(f"“{q}”" for q in job["queries"]))
    if not results:
        return

    saved_keys = {c["key"] for c in S.load_channels(dd)["channels"]}
    saved_ids = {c.get("channel_id") for c in S.load_channels(dd)["channels"]}
    fresh = [r for r in results if r["key"] not in saved_keys and r["channel_id"] not in saved_ids]
    picked = _results_table(job, readonly=False, saved_keys=saved_keys, saved_ids=saved_ids)

    st.write("")
    a1, a2 = st.columns([1, 2])
    each = a1.number_input("Shorts per run, each", 1, 500,
                           int(S.settings(dd).get("default_count", 10)), key="dc_each")
    rights = a2.checkbox("I have the rights to reuse these channels' Shorts", key="dc_rights")
    chosen = [r for r in fresh if r["channel_id"] in picked]
    c1, c2, c3 = st.columns(3)
    if c1.button(f"⭐ Add {len(chosen)} to wishlist", type="primary", width="stretch",
                 disabled=not (chosen and rights), key="dc_add"):
        added, problems = DS.add_to_wishlist(dd, chosen, int(each), rights_confirmed=rights,
                                             query_label=_job_label(job))
        _flash("success", f"Added {len(added)} channel(s) to your wishlist — see 📺 Channels.")
        for p in problems:
            _flash("warning", p)
        st.rerun()
    if c2.button(f"🙈 Hide {len(chosen)} for good", width="stretch", disabled=not chosen,
                 key="dc_dismiss", help="Never suggest these channels again."):
        DS.dismiss(dd, [r["channel_id"] for r in chosen])
        _flash("info", f"Hid {len(chosen)} channel(s) — they won't be suggested again.")
        st.rerun()
    if c3.button("🔁 Find more like these", width="stretch", key="dc_more",
                 help="Same search, skipping everything already shown."):
        p = dict(job.get("params") or {})
        p["exclude_ids"] = list(set(p.get("exclude_ids") or [])
                                | {r["channel_id"] for r in results})
        new = DS.new_job(dd, p)
        spawn_search(dd, new["id"])
        st.session_state["dc_view"] = new["id"]
        st.rerun()


def _lang_label(code: str | None) -> str:
    from ..shorts import langid as LG
    return LG.label(code or "")


def _pick_all(job: dict, value: bool) -> None:
    for r in job.get("results") or []:
        st.session_state[f"dc_pick_{job['id']}_{r['channel_id']}"] = value


def _results_table(job: dict, *, readonly: bool, saved_keys: set | None = None,
                   saved_ids: set | None = None) -> set[str]:
    """Results as cards. Returns the channel ids that are ticked."""
    picked: set[str] = set()
    saved_keys = saved_keys or set()
    saved_ids = saved_ids or set()
    results = job.get("results") or []
    if not readonly and results:
        s1, s2, _ = st.columns([1, 1, 4])
        s1.button("Select all", key=f"dc_all_{job['id']}", on_click=_pick_all,
                  args=(job, True), width="stretch")
        s2.button("Select none", key=f"dc_none_{job['id']}", on_click=_pick_all,
                  args=(job, False), width="stretch")
    for i, r in enumerate(results, 1):
        with st.container(border=True):
            c0, c1, c2 = st.columns([0.6, 5, 1.4])
            if r.get("avatar"):
                try:
                    c0.image(r["avatar"], width=48)
                except Exception:  # noqa: BLE001 - a missing picture is not an error
                    c0.write("📺")
            else:
                c0.write("📺")
            facts = [f"{_short_count(r['subscribers'])} subscribers" if r.get("subscribers")
                     else "subscribers hidden",
                     (r.get("country") or "country not listed")
                     + (" ✓" if r.get("country_status") == "match" else ""),
                     r.get("language_label") or "language unknown"]
            if r.get("fit") is not None:
                facts.append(f"fit {r['fit']}/10")
            c1.markdown(f"**{i}. [{r['name']}]({r['url']}/shorts)**  \n" + " · ".join(facts))
            if r.get("why"):
                c1.caption(r["why"])
            if r.get("sample_titles"):
                c1.caption("e.g. " + " | ".join(t[:60] for t in r["sample_titles"]))
            already = r["key"] in saved_keys or r["channel_id"] in saved_ids
            if readonly:
                continue
            if already:
                c2.markdown("✓ saved")
            elif c2.checkbox("Select", key=f"dc_pick_{job['id']}_{r['channel_id']}"):
                picked.add(r["channel_id"])
    return picked


def _tab_links(dd: str) -> None:
    st.caption("One-off downloads: paste Short (or video) links — they're not saved as "
               "channels. Anything already downloaded is skipped.")
    text = st.text_area("Links — one per line", height=160, key="sh_links",
                        placeholder="https://www.youtube.com/shorts/abcdefghijk\n"
                                    "https://youtu.be/abcdefghijk")
    links = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    rights = st.checkbox("These are my Shorts, or I have the rights to reuse them",
                         key="sh_links_rights")
    if st.button(f"⏬ Download {len(links) or ''} link(s)".replace("  ", " "),
                 type="primary", disabled=not (links and rights), key="sh_links_go"):
        q = S.start_run(dd, [], links)
        if q["links"]:
            _flash("success", f"Queued {q['links']} link(s). " + ensure_worker(dd))
            st.session_state.pop("sh_links", None)
        elif not q["bad_links"]:
            _flash("info", "Those links are already queued.")
        for b in q["bad_links"]:
            _flash("warning", f"Not a YouTube video link: {b}")
        st.rerun()

    st.write("")
    _quality_check(dd)


def _quality_check(dd: str) -> None:
    with st.expander("🔬 Check a Short's quality — what YouTube offers from this PC"):
        url = st.text_input("One Short's link", key="sh_qc_url",
                            placeholder="https://www.youtube.com/shorts/…")
        if st.button("Check", key="sh_qc_go", disabled=not url.strip()):
            with st.spinner("Asking YouTube for every version of this Short…"):
                try:
                    rows, best = YT.format_table(YT.probe_formats(url.strip(), Config.load()),
                                                 S.settings(dd))
                except Exception as e:  # noqa: BLE001 - show the reason, don't crash
                    st.error(str(e)[:400])
                    return
            if not rows:
                st.warning("YouTube listed no video versions for this link.")
                return
            st.success(f"Best on offer: **{best['width']}×{best['height']}** — the download "
                       f"will only accept this size or larger." if best else "")
            st.dataframe([{k: v for k, v in r.items() if k != "short"} for r in rows],
                         hide_index=True, width="stretch")


def _tab_downloads(dd: str) -> None:
    run = S.load_run(dd)
    notes = run.get("notes") or {}
    for key, note in notes.items():
        st.caption(f"ℹ️ **{key}** — {note}")
    items = run.get("items", [])
    if not items:
        st.info("Nothing in the current run. Start one from Channels or Paste links.")
        return
    rows = [{"": _STATUS_ICON.get(i.get("status"), "?"),
             "Channel": i.get("channel_name") or ("(pasted link)" if i.get("source") == "link"
                                                  else i.get("channel_key", "")),
             "Title": i.get("title") or i["id"],
             "Status": i.get("status"),
             "Problem": (i.get("error") or "")[:160],
             "Link": i.get("url")} for i in items]
    st.dataframe(rows, hide_index=True, width="stretch",
                 column_config={"Link": st.column_config.LinkColumn("Link", display_text="open")})
    if not S.has_work(run) and st.button("🧹 Clear this list", key="sh_clear_run",
                                         help="Only clears this view — every download "
                                              "stays in History."):
        S.clear_finished(dd)
        st.rerun()


def _short_side(r: dict) -> int:
    w, h = r.get("width") or 0, r.get("height") or 0
    return int(min(w, h)) if (w and h) else int(h or w or 0)


def _quality_label(r: dict) -> str:
    """"1080×1920 · 3.4 Mb/s" — resolution AND bitrate, because a file can be
    the right size and still look soft if it carries too little data."""
    size = f"{r.get('width') or '?'}×{r.get('height') or '?'}"
    br = r.get("bitrate_kbps")
    if not br and r.get("filesize") and r.get("duration"):
        br = round(int(r["filesize"]) * 8 / float(r["duration"]) / 1000)
    lab = size + (f" · {br / 1000:.1f} Mb/s" if br else "")
    return lab + (" ⚠️" if _is_low(r) else "")


def _is_low(r: dict) -> bool:
    return bool(r.get("quality_note")) or (0 < _short_side(r) < 1080)


def _redownload_panel(dd: str, items: list[dict]) -> None:
    """Fix Shorts that already came in pixelated: download them again at the
    best quality, each replacing its old copy under the SAME number."""
    low = [r for r in items if _is_low(r)]
    if low:
        st.warning(f"⚠️ {len(low)} Short(s) are below full quality (under 1080p, or YouTube "
                   f"cut the better stream off). Earlier versions could save a lower copy "
                   f"without saying so; this one won't. Re-download them to replace them.")
        if st.button(f"🔁 Re-download the {len(low)} low-quality Short(s) in best quality",
                     type="primary", key="sh_redl_low", width="stretch"):
            n = S.queue_redownload(dd, low)
            _flash("success", f"Queued {n} re-download(s) — each replaces its old copy and "
                              f"keeps its number. " + ensure_worker(dd))
            st.rerun()
    with st.expander("🔁 Re-download any Short in best quality"):
        labels = {f"#{r.get('number') or '-'} · {r.get('channel_name') or '-'} — "
                  f"{(r.get('title') or '')[:60]} ({_quality_label(r)})": r for r in items[:500]}
        picked = st.multiselect("Which ones?", list(labels), key="sh_redl_pick")
        if st.button(f"🔁 Re-download {len(picked) or ''} selected".replace("  ", " "),
                     disabled=not picked, key="sh_redl_go"):
            n = S.queue_redownload(dd, [labels[k] for k in picked])
            _flash("success", f"Queued {n} re-download(s). " + ensure_worker(dd))
            st.rerun()


def _tab_history(dd: str, root: str) -> None:
    items = sorted(S.history_items(dd).values(),
                   key=lambda r: r.get("downloaded_at") or 0, reverse=True)
    total_bytes = sum(int(r.get("filesize") or 0) for r in items)
    c1, c2, c3 = st.columns(3)
    c1.metric("Downloaded", len(items))
    c2.metric("On disk", _mb(total_bytes))
    c3.metric("Channels", len({r.get("channel_key") or r.get("channel_name") for r in items}))
    if not items:
        st.info("Nothing downloaded yet.")
        return

    f1, f2 = st.columns([2, 1])
    q = f1.text_input("Search titles, hashtags and channels", key="sh_hist_q").strip().lower()
    chans = ["All channels"] + sorted({r.get("channel_name") or "-" for r in items})
    pick = f2.selectbox("Channel", chans, key="sh_hist_ch")
    shown = [r for r in items
             if (pick == "All channels" or (r.get("channel_name") or "-") == pick)
             and (not q or q in (r.get("title", "") + " " + " ".join(r.get("hashtags") or [])
                                 + " " + (r.get("channel_name") or "")).lower())]
    rows = [{"#": r.get("number") or None,
             "Downloaded": time.strftime("%Y-%m-%d %H:%M",
                                         time.localtime(r.get("downloaded_at") or 0)),
             "Channel": r.get("channel_name") or "-",
             "Title": r.get("title"),
             "Quality": _quality_label(r),
             "Size": _mb(r.get("filesize")),
             "Hashtags": " ".join(r.get("hashtags") or []),
             "Link": r.get("url")} for r in shown]
    st.dataframe(rows, hide_index=True, width="stretch", height=min(600, 38 + 35 * len(rows)),
                 column_config={"Link": st.column_config.LinkColumn("Link", display_text="open")})

    _redownload_panel(dd, items)

    def export_csv(rows=tuple(shown)) -> bytes:
        # Built when the button is clicked, not on every rerun of the page.
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["downloaded", "channel", "title", "description", "hashtags", "width",
                    "height", "bytes", "file", "url"])
        for r in rows:
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(r.get("downloaded_at") or 0)),
                        r.get("channel_name"), r.get("title"), r.get("description"),
                        " ".join(r.get("hashtags") or []), r.get("width"), r.get("height"),
                        r.get("filesize"), r.get("file"), r.get("url")])
        return buf.getvalue().encode("utf-8-sig")

    d1, d2 = st.columns(2)
    d1.download_button("⬇️ Export this list (CSV)", export_csv,
                       file_name="shorts_history.csv", mime="text/csv", width="stretch")
    if d2.button("📂 Open the Shorts folder", width="stretch", key="sh_hist_open"):
        ok, err = _open_folder(root)
        if not ok:
            st.error(f"Couldn't open it: {err}\n\n{root}")

    with st.expander("📝 Title, description and hashtags of one Short"):
        labels = [f"{r.get('channel_name') or '-'} — {r.get('title', '')[:70]}" for r in shown[:300]]
        if labels:
            idx = labels.index(st.selectbox("Which Short?", labels, key="sh_hist_pick"))
            r = shown[idx]
            st.text_input("Title", r.get("title", ""), key=f"sh_t_{r['id']}")
            st.text_area("Description", r.get("description", ""), height=140,
                         key=f"sh_d_{r['id']}")
            st.text_input("Hashtags", " ".join(r.get("hashtags") or []), key=f"sh_h_{r['id']}")
            srcs = []
            if r.get("description_source") != "uploader":
                srcs.append("the description was written by ShortForge (the uploader left none)")
            if r.get("hashtags_source") in ("generated", "ai"):
                srcs.append("the hashtags were written by ShortForge (the uploader used none)")
            if srcs:
                st.caption("Note: " + "; ".join(srcs) + ".")
            st.caption(f"File: `{r.get('file')}`")
            if st.button("↩️ Allow this Short to be downloaded again", key=f"sh_forget_{r['id']}"):
                S.forget(dd, r["id"])
                if r.get("file") and os.path.isfile(r["file"]) and \
                        f"[{r['id']}]" in os.path.basename(r["file"]):
                    st.warning("Removed from history. This older file still has the video's "
                               "id in its name, so it will still be skipped — delete or move "
                               "it (or run 🧹 Tidy in Settings) to allow a re-download.")
                else:
                    st.success("Removed from history — the next download can fetch it again.")


def _tab_settings(dd: str, cfg: Config) -> None:
    s = S.settings(dd)
    st.caption("Saved on disk when you press Save — they stay exactly as you left them.")
    out = st.text_input("Save Shorts to this folder",
                        value=s.get("output_dir") or YT.output_root(cfg, s),
                        help="Leave as-is to use the program's output folder.")
    per_ch = st.toggle("A folder for each channel", value=bool(s.get("folder_per_channel", True)))
    default_count = st.number_input("Shorts per run for newly added channels", min_value=1,
                                    max_value=500, value=int(s.get("default_count", 10)))
    q_keys = list(_QUALITY)
    quality = st.selectbox("Quality", q_keys, q_keys.index(str(s.get("max_height", "best")))
                           if str(s.get("max_height", "best")) in q_keys else 0,
                           format_func=lambda k: _QUALITY[k],
                           help="Best available downloads the original streams as YouTube "
                                "serves them — no re-encoding, no scaling.")
    allow_lower = st.toggle(
        "If the best quality can't be downloaded, save a lower-quality copy instead",
        value=bool(s.get("allow_lower_quality", False)),
        help="OFF (recommended): a Short is only saved at the best quality YouTube "
             "offers. If YouTube keeps cutting that stream off, the Short is marked failed "
             "(Retry tries again) — you never get a pixelated copy without knowing. ON: "
             "step down one size at a time and label the copy in History.")
    c_keys = list(_CODEC)
    codec = st.radio("Codec", c_keys, c_keys.index(s.get("codec", "best"))
                     if s.get("codec", "best") in c_keys else 0,
                     format_func=lambda k: _CODEC[k])
    container = st.radio("File type", ["mp4", "mkv"],
                         0 if s.get("container", "mp4") != "mkv" else 1, horizontal=True,
                         help="mp4 plays almost everywhere. Streams are merged, never "
                              "re-encoded, in either case.")
    names = {"number_title": "1 - Title.mp4, 2 - Title.mp4, … (number + title)",
             "number": "1.mp4, 2.mp4, … (number only)"}
    nkeys = list(names)
    name_style = st.radio("File names", nkeys,
                          nkeys.index(s.get("name_style", "number_title"))
                          if s.get("name_style", "number_title") in nkeys else 0,
                          format_func=lambda k: names[k],
                          help="Numbered in the order they were downloaded, per folder, and "
                               "the count carries on: download 3, then 3 more, and the "
                               "second batch is 4, 5, 6.")
    st.markdown("**What goes in the folder** — by default, only the videos.")
    embed = st.toggle("Write the title, description and hashtags INTO each video",
                      value=bool(s.get("embed_metadata", True)),
                      help="No quality change (nothing is re-encoded). Windows shows them in "
                           "the file's Properties → Details. They're also always in History.")
    t1, t2, t3 = st.columns(3)
    txt = t1.toggle("Also save a .txt (title/description/hashtags)",
                    value=bool(s.get("sidecar_txt", False)))
    js = t2.toggle("Also save a .json (all details)", value=bool(s.get("sidecar_json", False)))
    thumb = t3.toggle("Also save the thumbnail (.jpg)",
                      value=bool(s.get("save_thumbnail", False)))
    ai = st.toggle("AI fills in missing descriptions/hashtags",
                   value=bool(s.get("ai_fill_missing", False)),
                   help="Only when the uploader left them empty. Uses the model bound to "
                        "'metadata' in Task routing. Off = ShortForge builds them from the "
                        "title, and says so.")
    n1, n2 = st.columns(2)
    delay = n1.number_input("Pause between downloads (seconds)", min_value=0, max_value=30,
                            value=int(s.get("delay_seconds", 2)),
                            help="A short pause makes YouTube's bot check less likely.")
    wall = n2.number_input("Pause the run after this many bot-check failures in a row",
                           min_value=1, max_value=20, value=int(s.get("stop_on_bot_wall", 3)))
    if st.button("💾 Save settings", type="primary", key="sh_save_settings"):
        chosen = out.strip()
        S.update_settings(dd, output_dir=("" if chosen == YT.output_root(cfg, {**s, "output_dir": ""})
                                          else chosen),
                          folder_per_channel=per_ch, default_count=int(default_count),
                          max_height=quality, codec=codec, container=container,
                          save_thumbnail=thumb, sidecar_txt=txt, sidecar_json=js,
                          name_style=name_style, embed_metadata=embed, ai_fill_missing=ai,
                          allow_lower_quality=allow_lower,
                          delay_seconds=int(delay), stop_on_bot_wall=int(wall))
        st.success("Saved.")

    st.divider()
    st.markdown("**🧹 Tidy the Shorts folder** — for downloads made before this update: "
                "removes the extra .txt/.json/.jpg files and unfinished .part pieces, and "
                "numbers the videos in the order they were downloaded. Only files this "
                "program named are touched. You see the list before anything changes.")
    from ..shorts import tidy as TD
    if st.button("🔍 Check what can be tidied", key="sh_tidy_check"):
        st.session_state["sh_tidy_plan"] = TD.plan(dd, cfg)
    plan = st.session_state.get("sh_tidy_plan")
    if plan:
        st.info(TD.describe(plan))
        if plan["deletes"] or plan["renames"]:
            with st.expander(f"See the {len(plan['deletes'])} removal(s) and "
                             f"{len(plan['renames'])} rename(s)"):
                for r in plan["renames"][:200]:
                    st.text(f"rename  {os.path.basename(r['from'])}\n     →  "
                            f"{os.path.basename(r['to'])}")
                for path, why in plan["deletes"][:300]:
                    st.text(f"remove  {os.path.basename(path)}   ({why})")
        if plan["deletes"] or plan["renames"] or plan["stale_staging"]:
            if st.button("🧹 Tidy now", type="primary", key="sh_tidy_go"):
                done = TD.apply(dd, plan)
                st.session_state.pop("sh_tidy_plan", None)
                _flash("success", f"Tidied: removed {done['removed']} file(s), numbered "
                                  f"{done['renamed']} video(s).")
                st.rerun()

    st.divider()
    st.markdown("**Backup** — your channel list and settings in one file.")
    b1, b2 = st.columns(2)
    try:
        with open(os.path.join(dd, S.CHANNELS_FILE), "rb") as f:
            b1.download_button("⬇️ Export channels + settings", f.read(),
                               file_name="shorts_channels.json", mime="application/json",
                               width="stretch")
    except OSError:
        b1.caption("Nothing to export yet.")
    up = b2.file_uploader("Import a backup", type=["json"], key="sh_import",
                          label_visibility="collapsed")
    if up is not None and st.button("Import — adds channels, keeps existing ones",
                                    key="sh_import_go"):
        import json
        try:
            data = json.loads(up.getvalue().decode("utf-8"))
            lines = "\n".join(c["url"] for c in data.get("channels", []) if c.get("url"))
            added, _ = S.add_channels(dd, lines, int(s.get("default_count", 10)),
                                      rights_confirmed=True)
            counts = {c["key"]: c.get("count") for c in data.get("channels", [])}
            for c in added:
                if counts.get(c["key"]):
                    S.update_channel(dd, c["key"], count=int(counts[c["key"]]))
            st.success(f"Imported {len(added)} new channel(s).")
        except (ValueError, KeyError) as e:
            st.error(f"That file isn't a Shorts backup: {e}")

    log_path = os.path.join(dd, S.LOG_FILE)
    if os.path.isfile(log_path):
        with st.expander("🪵 Downloader log (last 60 lines)"):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    st.code("".join(f.readlines()[-60:]) or "(empty)")
            except OSError as e:
                st.caption(f"Couldn't read the log: {e}")


_js_seen: tuple[float, bool] = (0.0, False)


def _has_js_runtime() -> bool:
    """Is Deno (or Node/Bun) installed? Looking means searching every folder on
    PATH, which on Windows is hundreds of file checks — so once found it is
    remembered, and a "no" is looked at again every 30 seconds (so the warning
    goes away soon after update.bat installs Deno, without a restart)."""
    global _js_seen
    from ..ingest.ingest import find_js_runtime
    at, ok = _js_seen
    if ok or time.time() - at < 30:
        return ok
    _js_seen = (time.time(), find_js_runtime() is not None)
    return _js_seen[1]


def render(run_every: str = "3s") -> None:
    cfg = Config.load()
    dd = S.data_dir(cfg)
    root = YT.output_root(cfg, S.settings(dd))

    st.header("📥 YouTube Shorts downloader")
    st.caption("Save channels, give each one its own count, and download that many of "
               "their newest Shorts at the best quality YouTube offers — never the same "
               "Short twice, each with its title, description and hashtags saved beside it.")
    if not _has_js_runtime():
        st.warning("⚠️ **Deno is not installed**, so YouTube may hide the best-quality "
                   "version of a Short (it shows the full list only to programs that can "
                   "run its JavaScript). Run **update.bat** once — it installs Deno — then "
                   "restart ShortForge.")

    _show_flash()
    st.fragment(run_every=run_every)(_status_panel)(dd)

    # Only the open tab is built. Streamlit otherwise runs every tab on every
    # click — all channel rows, the whole download history, past searches —
    # and switching between them is what dragged.
    t1, t0, t2, t3, t4, t5 = lazy_tabs(["📺 Channels", "🔎 Find channels", "🔗 Paste links",
                                        "⏬ This run", "🕘 History", "⚙️ Settings"],
                                       key="sh_tab")
    if t1.open:
        with t1:
            _tab_channels(dd)
    if t0.open:
        with t0:
            _tab_find(dd, run_every)
    if t2.open:
        with t2:
            _tab_links(dd)
    if t3.open:
        with t3:
            st.fragment(run_every=run_every)(_tab_downloads)(dd)
    if t4.open:
        with t4:
            _tab_history(dd, root)
    if t5.open:
        with t5:
            _tab_settings(dd, cfg)
