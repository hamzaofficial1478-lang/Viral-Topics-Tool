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
    m4.metric("All-time downloads", len(S.load_history(dd)["items"]))

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

def _on_count(dd: str, key: str, widget: str) -> None:
    S.update_channel(dd, key, count=int(st.session_state[widget]))


def _on_toggle(dd: str, key: str, widget: str) -> None:
    S.update_channel(dd, key, enabled=bool(st.session_state[widget]))


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
            st.rerun()

    if not chans:
        st.info("No channels saved yet. Add some above — they're kept on disk, "
                "so they are still here next time you open the program.")
        return

    enabled = [c for c in chans if c.get("enabled", True)]
    total = sum(int(c.get("count", 0)) for c in enabled)
    h1, h2, h3 = st.columns([3, 1, 1])
    h1.markdown(f"**{len(enabled)} of {len(chans)} channel(s) switched on** — a download "
                f"takes up to **{total}** new Short(s), newest first, skipping any "
                f"already downloaded.")
    if h2.button("Turn all on", width="stretch", disabled=len(enabled) == len(chans)):
        S.set_all_enabled(dd, True)
        st.rerun()
    if h3.button("Turn all off", width="stretch", disabled=not enabled):
        S.set_all_enabled(dd, False)
        st.rerun()

    for i, c in enumerate(chans, 1):
        key = c["key"]
        with st.container(border=True):
            r1, r2, r3, r4 = st.columns([0.7, 4, 1.6, 0.8])
            tw = f"sh_on_{key}"
            on = bool(c.get("enabled", True))
            r1.toggle(f"Include {key}", value=on, key=tw, label_visibility="collapsed",
                      on_change=_on_toggle, args=(dd, key, tw))
            name = c.get("name") or key
            state = ("" if on else " · <span style='color:#b45309'>switched off — "
                     "skipped, but its count is kept</span>")
            r2.markdown(f"**{i}. {name}**  \n[{key}]({c['url']}/shorts) · "
                        f"{taken.get(key, 0)} downloaded so far{state}",
                        unsafe_allow_html=True)
            cw = f"sh_cnt_{key}"
            r3.number_input("Per run", min_value=1, max_value=500,
                            value=int(c.get("count", 10)), key=cw,
                            on_change=_on_count, args=(dd, key, cw),
                            help="How many NEW Shorts to take from this channel each "
                                 "time you download. Saved the moment you change it.")
            confirm = f"sh_rm_{key}"
            if st.session_state.get(confirm):
                if r4.button("Sure?", key=f"sh_rm_yes_{key}", type="primary",
                             help="Removes the channel. Its downloads and history stay."):
                    S.remove_channel(dd, key)
                    st.session_state.pop(confirm, None)
                    st.rerun()
            elif r4.button("🗑", key=f"sh_rm_{key}_btn", help="Remove this channel"):
                st.session_state[confirm] = True
                st.rerun()

    st.write("")
    if st.button(f"⏬ Download {total} new Short(s) from {len(enabled)} channel(s)",
                 type="primary", width="stretch", disabled=not enabled, key="sh_go"):
        q = S.start_run(dd, [c["key"] for c in enabled], [])
        _flash("success", f"Queued {q['channels']} channel(s). " + ensure_worker(dd))
        st.rerun()


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
    items = sorted(S.load_history(dd)["items"].values(),
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
    rows = [{"#": r.get("number") or "",
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

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["downloaded", "channel", "title", "description", "hashtags", "width",
                "height", "bytes", "file", "url"])
    for r in shown:
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.get("downloaded_at") or 0)),
                    r.get("channel_name"), r.get("title"), r.get("description"),
                    " ".join(r.get("hashtags") or []), r.get("width"), r.get("height"),
                    r.get("filesize"), r.get("file"), r.get("url")])
    d1, d2 = st.columns(2)
    d1.download_button("⬇️ Export this list (CSV)", buf.getvalue().encode("utf-8-sig"),
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


def render(run_every: str = "3s") -> None:
    cfg = Config.load()
    dd = S.data_dir(cfg)
    root = YT.output_root(cfg, S.settings(dd))

    st.header("📥 YouTube Shorts downloader")
    st.caption("Save channels, give each one its own count, and download that many of "
               "their newest Shorts at the best quality YouTube offers — never the same "
               "Short twice, each with its title, description and hashtags saved beside it.")

    _show_flash()
    st.fragment(run_every=run_every)(_status_panel)(dd)

    t1, t2, t3, t4, t5 = st.tabs(["📺 Channels", "🔗 Paste links", "⏬ This run",
                                  "🕘 History", "⚙️ Settings"])
    with t1:
        _tab_channels(dd)
    with t2:
        _tab_links(dd)
    with t3:
        st.fragment(run_every=run_every)(_tab_downloads)(dd)
    with t4:
        _tab_history(dd, root)
    with t5:
        _tab_settings(dd, cfg)
