#!/usr/bin/env python3
"""ShortForge web dashboard — a thin Streamlit layer over the pipeline.

Launch with ``python cli.py ui``. Screens: 📹 Long videos (one list of links:
add, tick, process — each link keeps its own settings), 📥 Shorts, 🐛 Error
logs, 📚 History and ⚙️ Settings. Clip jobs run in a separate worker process
(`cli.py queue run`, or the one `start_all.bat` starts), so closing the browser
never stops them.
"""

from __future__ import annotations

import datetime
import glob
import json
import logging
import os
import re
import subprocess
import sys
import time

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    raise SystemExit("Streamlit is not installed. Run: pip install streamlit")

from shortforge.captions.templates import ANIMATIONS, list_templates
from shortforge.config import Config
from shortforge.utils import load_env_file, setup_logging

# Map log-line keywords to a progress fraction + friendly stage label.
_STAGES = [
    ("ingest", 0.10, "Ingesting source"),
    ("transcrib", 0.25, "Transcribing"),
    ("hook scoring", 0.38, "Scoring hooks"),
    ("hook detection", 0.42, "Finding the best moments"),
    ("selected", 0.52, "Selecting clips"),
    ("logo removal: scanning", 0.56, "Looking for logos/watermarks"),
    ("virtual camera", 0.62, "Reframing (virtual camera)"),
    ("translating", 0.68, "Translating"),
    ("stems", 0.72, "Separating music/voice"),
    ("rendering clip", 0.85, "Rendering"),
    ("wrote manifest", 1.0, "Done"),
]

_MANIFEST_GLOB = "*_manifest.json"




_THEME_CSS = """
<style>
/* ShortForge — branded, minimal. Minimal means few elements and generous
   space, NOT desaturated: the first attempt read it as grey and came out
   lifeless. Colour carries the brand (indigo -> violet), restraint comes from
   using it in exactly two places: the primary action, and the rule under a
   section heading. Everything else is ink, hairline and shadow. */
:root {
  --sf-ink:      #111827;
  --sf-muted:    #6B7280;
  --sf-faint:    #9CA3AF;
  --sf-line:     #E9EAEF;
  --sf-accent:   #6366F1;
  --sf-accent-2: #8B5CF6;
  --sf-grad:     linear-gradient(135deg, #6366F1 0%, #8B5CF6 100%);
  --sf-glow:     0 6px 16px -4px rgba(99,102,241,.42);
  --sf-card:     0 1px 2px rgba(16,24,40,.04), 0 4px 12px -6px rgba(16,24,40,.08);
  --sf-r:        12px;
}
.block-container { padding-top: 2rem; padding-bottom: 4rem; max-width: 1120px; }
html, body, [class*="css"] { -webkit-font-smoothing: antialiased; }

/* ---- Typography: real hierarchy, tighter tracking on headings ---- */
h1 { font-weight: 700 !important; letter-spacing: -.03em; font-size: 2rem !important; }
h2 { font-weight: 650 !important; letter-spacing: -.02em; font-size: 1.35rem !important;
     margin-top: 2rem !important; }
h3 { font-weight: 600 !important; letter-spacing: -.01em; }
/* Accent rule under section headings — the brand, used sparingly. */
h2::after {
  content: ""; display: block; width: 34px; height: 3px; margin-top: .5rem;
  background: var(--sf-grad); border-radius: 3px;
}

/* ---- Buttons: gradient primary that lifts, crisp ghost secondary ---- */
.stButton > button {
  border-radius: var(--sf-r);
  border: 1px solid var(--sf-line);
  background: #FFFFFF;
  color: var(--sf-ink);
  font-weight: 600;
  font-size: .875rem;
  letter-spacing: -.005em;
  padding: .55rem 1.1rem;
  box-shadow: var(--sf-card);
  transition: transform .12s ease, box-shadow .18s ease,
              border-color .18s ease, background .18s ease;
}
.stButton > button:hover:enabled {
  transform: translateY(-1px);
  border-color: #D6D9E3;
  box-shadow: 0 4px 14px -4px rgba(16,24,40,.16);
}
.stButton > button:active:enabled { transform: translateY(0); }
.stButton > button[kind="primary"] {
  background: var(--sf-grad);
  border: none;
  color: #FFFFFF;
  box-shadow: var(--sf-glow);
}
.stButton > button[kind="primary"]:hover:enabled {
  filter: brightness(1.06);
  box-shadow: 0 10px 22px -6px rgba(99,102,241,.55);
}
.stButton > button:disabled { opacity: .4; box-shadow: none; }
.stDownloadButton > button { border-radius: var(--sf-r); font-weight: 600; }

/* ---- Inputs: soft, with a branded focus ring ---- */
.stTextInput input, .stNumberInput input, .stTextArea textarea,
.stSelectbox div[data-baseweb="select"] > div,
.stChatInput textarea {
  border-radius: var(--sf-r) !important;
  border-color: var(--sf-line) !important;
  background: #FFFFFF !important;
}
.stTextInput input:focus, .stNumberInput input:focus,
.stTextArea textarea:focus, .stChatInput textarea:focus {
  border-color: var(--sf-accent) !important;
  box-shadow: 0 0 0 3px rgba(99,102,241,.13) !important;
}
label, .stCheckbox label { font-weight: 500 !important; color: #374151; }

/* ---- Cards: elevation instead of borders ---- */
div[data-testid="stMetric"] {
  background: #FFFFFF;
  border: 1px solid var(--sf-line);
  border-radius: var(--sf-r);
  padding: .85rem 1rem;
  box-shadow: var(--sf-card);
}
div[data-testid="stMetricLabel"] {
  color: var(--sf-faint); font-size: .72rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .06em;
}
div[data-testid="stMetricValue"] { font-weight: 680; letter-spacing: -.02em; }
.stExpander {
  border: 1px solid var(--sf-line); border-radius: var(--sf-r);
  background: #FFFFFF; box-shadow: var(--sf-card);
}
.stDataFrame { border-radius: var(--sf-r); overflow: hidden; border: 1px solid var(--sf-line); }
.stCodeBlock, pre {
  border-radius: var(--sf-r) !important; font-size: .8rem;
  background: #F7F8FA !important; border: 1px solid var(--sf-line) !important;
}

/* ---- Alerts: tinted, accent-led, no heavy chrome ---- */
.stAlert {
  border: none !important; border-left: 3px solid var(--sf-accent) !important;
  border-radius: 10px !important; box-shadow: var(--sf-card);
}

/* ---- Sidebar: the branded rail; active screen carries the gradient ---- */
section[data-testid="stSidebar"] {
  background: #FFFFFF; border-right: 1px solid var(--sf-line);
}
section[data-testid="stSidebar"] .stRadio label {
  padding: .4rem .6rem; border-radius: 9px; font-weight: 550;
  transition: background .15s ease;
}
section[data-testid="stSidebar"] .stRadio label:hover { background: #F4F4F8; }

/* ---- Progress: gradient, slim ---- */
.stProgress > div > div > div > div {
  background-image: var(--sf-grad); border-radius: 99px;
}
.stProgress > div > div > div { height: 7px; border-radius: 99px; background: #EEF0F4; }

hr { border-color: var(--sf-line); margin: 1.6rem 0; }
/* Hide the "Made with Streamlit" badge, but keep the hamburger: its Rerun and
   Clear-cache entries are the escape hatch when the page wedges. */
footer { visibility: hidden; }

/* ---- Resource strip: small, right-aligned, out of the way ---- */
.sf-res {
  text-align: right; color: var(--sf-faint);
  font-size: .74rem; line-height: 1.6; font-variant-numeric: tabular-nums;
  padding-top: .4rem; border-top: 1px solid var(--sf-line); margin-top: 2rem;
}
.sf-res b { color: var(--sf-muted); font-weight: 600; }
.sf-dot { color: var(--sf-line); padding: 0 .35rem; }
</style>
"""


@st.fragment(run_every="10s")
def _resource_footer() -> None:
    """One small right-aligned line of what ShortForge is costing the machine.

    Deliberately NOT metric cards: this is reference information you glance at,
    not a headline, and four big boxes gave it the same visual weight as the
    queue itself. It still answers the recurring "is it stuck or just slow?" —
    CPU moving means work is happening — but from the corner of the eye.

    Every value degrades to "?" rather than raising: a monitoring line must
    never be able to break the thing it monitors.
    """
    from shortforge import resources as R
    from shortforge.version import build_id
    from shortforge.config import Config as _C

    work_dir = _C.load().get("paths.work_dir", ".shortforge")
    s = R.snapshot(work_dir)
    dot = '<span class="sf-dot">•</span>'

    cpu = s.get("proc_cpu_pct")
    parts = [
        # First, because it is the one thing that tells the operator whether
        # this PC actually has the code they pulled on the other one.
        f"build <b>{build_id()}</b>",
        f"ShortForge <b>{R.human_bytes(s.get('proc_mem'))}</b> RAM",
        f"<b>{cpu:.0f}%</b> CPU" if cpu is not None else "CPU <b>?</b>",
        f"RAM <b>{R.human_bytes(s.get('mem_used'))}</b>/{R.human_bytes(s.get('mem_total'))}",
        f"<b>{s.get('cores') or '?'}</b> cores",
        f"disk <b>{R.human_bytes(s.get('disk_free'))}</b> free",
    ]
    gpu = R.gpu_name_nowait()              # never waits: asking Windows takes ~1s
    if gpu and gpu != "not detected":
        parts.append(f"GPU <b>{gpu[:28]}</b>")
    speed = R.render_speed(work_dir)
    if speed:
        parts.append(f"⏱ <b>{speed}</b>")
    if not s.get("has_psutil"):
        parts.append("<i>pip install psutil for per-process figures</i>")

    st.markdown(f'<div class="sf-res">{dot.join(parts)}</div>', unsafe_allow_html=True)


def _inject_theme() -> None:
    """Apply the soft/minimal styling. Pure presentation — no behaviour."""
    st.markdown(_THEME_CSS, unsafe_allow_html=True)


def _open_folder(path: str) -> tuple[bool, str]:
    """Open a folder in the OS file manager (Explorer on Windows). The app runs
    on the operator's own machine, so this opens it locally."""
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _stage_from_lines(lines: list[str]) -> tuple[float, str]:
    """Furthest-along stage seen in the log so far (monotonic-ish progress)."""
    frac, label = 0.05, "Working…"
    low = [ln.lower() for ln in lines]
    for key, f, lab in _STAGES:
        if any(key in ln for ln in low):
            frac, label = f, lab
    return frac, label


def _render_warnings(warnings: list, limit: int | None = None) -> None:
    items = warnings[-limit:] if limit else warnings
    for level, msg in items:
        (st.error if level >= logging.ERROR else st.warning)(msg)


# --------------------------------------------------------------------------- #
#  Results rendering (shared by the live result view and the History screen)   #
# --------------------------------------------------------------------------- #

def _render_folder_bar(folder: str, key_prefix: str) -> None:
    st.markdown("**Output folder**")
    c1, c2 = st.columns([4, 1])
    with c1:
        st.code(folder or "out")               # copyable path
    with c2:
        if st.button("📂 Open folder", key=f"open_{key_prefix}", width="stretch"):
            ok, err = _open_folder(folder)
            if not ok:
                st.warning(f"Couldn't open it from here ({err}) — copy the path on the left.")


def _render_qc(manifest: dict) -> None:
    s = manifest.get("settings", {})
    target_lufs = Config.load().get("render.loudnorm_i", -14.0)
    parts = []
    if s.get("loudnorm", True):
        parts.append(f"🔊 Loudness normalized to ~{target_lufs:.0f} LUFS (TP ≤ −1 dBTP)")
    else:
        parts.append("🔊 Loudness normalization OFF")
    if s.get("resolution"):
        parts.append(f"🖼 {s['resolution']} · {s.get('aspect', '?')} · "
                     f"reframe {s.get('reframe_mode', '?')}")
    flagged = (manifest.get("review") or {}).get("flagged", 0)
    if flagged:
        parts.append(f"⚠️ {flagged} clip(s) flagged for review")
    ce = manifest.get("cost_estimate") or {}
    if ce.get("priced"):
        parts.append(f"💲 est ${ce.get('cost_usd', 0):.2f}")
    st.caption("  ·  ".join(parts))


def _copyable(label: str, text: str, key: str) -> None:
    """A field with Streamlit's built-in copy button (the ⧉ on a code block)."""
    if not text:
        return
    st.caption(label)
    st.code(text, language=None)


def _file_reader(path: str):
    """Download-button data that is read only when the button is CLICKED.

    Passing the bytes read the whole clip from disk on every rerun of the page,
    for every clip shown — with a few dozen past runs that was gigabytes per
    click on the History screen.
    """
    def read() -> bytes:
        with open(path, "rb") as f:
            return f.read()
    return read


def _render_clip(c: dict, key: str, *, video_on_demand: bool = False) -> None:
    """One clip: player, download, thumbnail, and its copy-ready text.

    ``video_on_demand``: show a ▶ Play button instead of the player. Streamlit
    reads and hashes the WHOLE video file every time a page with ``st.video``
    reruns, so a screen of old runs only loads the clip you ask to watch.
    """
    vc, ic = st.columns([2, 3])
    fp = c.get("file_path", "")
    with vc:
        if fp and os.path.isfile(fp):
            play_key = f"play_{key}"
            if not video_on_demand or st.session_state.get(play_key):
                st.video(fp)
            elif st.button("▶ Play", key=f"{play_key}_btn", width="stretch"):
                st.session_state[play_key] = True
                st.rerun()
            st.download_button("⬇ Download clip", _file_reader(fp), os.path.basename(fp),
                               mime="video/mp4", key=f"dl_{key}", width="stretch")
        else:
            st.warning("Clip file not found on disk.")
        thumb = c.get("thumbnail")
        if thumb and os.path.isfile(thumb):
            st.image(thumb, caption="Thumbnail (cover frame)", width="stretch")
    with ic:
        dur = c.get("duration") or (c.get("end", 0) - c.get("start", 0))
        st.markdown(f"**Clip {c.get('clip_id', '?')}** — "
                    f"{c.get('start', 0):.1f}–{c.get('end', 0):.1f}s ({dur:.1f}s)  ·  "
                    f"hook score **{c.get('score', 0):.2f}**")
        if c.get("reason"):
            st.caption(f"Why this moment: {c['reason']}")
        review = c.get("review") or {}
        if review.get("flags"):
            st.warning("QC flags: " + ", ".join(review["flags"]))
        md = c.get("metadata") or {}
        _copyable("Title", md.get("title", ""), f"t_{key}")
        _copyable("Description", md.get("description", ""), f"d_{key}")
        if md.get("tags"):
            _copyable("Tags", ", ".join(md["tags"]), f"tag_{key}")
        if md.get("hashtags"):
            _copyable("Hashtags", " ".join(md["hashtags"]), f"h_{key}")
        pubf = c.get("publish_file")
        if pubf and os.path.isfile(pubf):
            st.download_button("⬇ title/desc/tags (.txt)", _file_reader(pubf),
                               os.path.basename(pubf), key=f"sc_{key}")


def _render_timings(manifest: dict) -> None:
    """STEP 0: per-stage timing breakdown + every ffmpeg call, so the bottleneck
    is visible on the page (not just in the console)."""
    t = manifest.get("timings")
    line = manifest.get("timings_summary")
    if not t and not line:
        return
    if line:
        st.caption("⏱ Per-stage timing")
        st.code(line, language=None)
    if not t:
        return
    stages = t.get("stages") or []
    if stages:
        rows = [{"stage": s.get("stage"),
                 "seconds": ("" if s.get("seconds") is None else s.get("seconds")),
                 "note": s.get("note") or ""} for s in stages]
        with st.expander(f"Timing detail · total {t.get('total_seconds', 0):.0f}s", expanded=False):
            st.dataframe(rows, width="stretch", hide_index=True)
            calls = t.get("ffmpeg_calls") or []
            if calls:
                st.caption(f"{len(calls)} ffmpeg call(s), slowest first")
                slow = sorted(calls, key=lambda c: c.get("seconds", 0), reverse=True)
                st.dataframe([{"seconds": c.get("seconds"), "cmd": c.get("cmd")} for c in slow],
                             width="stretch", hide_index=True)


def _render_results(manifest: dict, out_dir: str | None = None,
                    key_prefix: str = "live", *, video_on_demand: bool = False) -> None:
    st.success(manifest.get("summary", "Done"))
    _render_qc(manifest)
    _render_timings(manifest)
    folder = out_dir or os.path.dirname(manifest.get("manifest_path", "")) \
        or os.path.abspath("out")
    _render_folder_bar(folder, key_prefix)
    rationale = (manifest.get("recommendation") or {}).get("rationale")
    if rationale:
        st.info(rationale)
    clips = manifest.get("clips", [])
    st.markdown(f"### {len(clips)} clip(s)")
    for i, c in enumerate(clips):
        st.divider()
        _render_clip(c, f"{key_prefix}_{i}", video_on_demand=video_on_demand)


def _worker_is_live(work_dir: str, *, freshness_s: float = 30.0) -> bool:
    """Is a queue worker (``cli.py listen``) already running and checking in?

    Thin wrapper over `lifecycle.worker_is_live` — one implementation shared
    with ntfy's /status, so the dashboard and the phone can never disagree
    about whether a worker exists on this machine.
    """
    from shortforge import lifecycle
    return lifecycle.worker_is_live(work_dir, freshness_s=freshness_s)


def _start_worker() -> None:
    """Launch the queue runner as its own process so it survives UI reruns and
    keeps going even if you close the browser tab.

    Only for the standalone-dashboard case — see `_begin_working`, which never
    calls this while `cli.py listen` is already running.
    """
    from shortforge import runner
    st.session_state.queue_proc = runner.spawn_worker(os.path.dirname(os.path.abspath(__file__)))


def _begin_working(work_dir: str) -> str:
    """Un-pause the queue and make sure something is working it. Returns the
    message to show.

    Two bugs live here, both reported by the operator as "the Start working
    button is broken — it dropped three messages at once and my link never
    started":

    1. **Order.** The un-pause MUST be on disk before any worker process reads
       it. An earlier version flipped `paused` in memory, spawned the worker,
       and only then saved — so the child read `queue.json` while it still said
       `paused: true`, `drain_queue` broke out instantly, and the run reported
       "All links processed" over a link it had never touched.
    2. **Don't spawn a second worker.** `start_all.bat` already runs
       `cli.py listen`, whose worker thread drains the queue by itself. Also
       spawning `cli.py queue run` produced a redundant process that fought for
       the queue lock, overwrote the listener's `runstate.json`, and announced
       its own start/finish/shutdown — three phone messages from a helper that
       did no work. When a live worker exists, unpausing is the whole job.
    """
    from shortforge import queue as Q

    fresh = Q.load_queue(work_dir)          # never a stale snapshot
    Q.set_paused(fresh, False)
    Q.save_queue(fresh, work_dir)           # ON DISK before anything reads it

    if _worker_is_live(work_dir):
        return ("Started — the running worker picks this up within a few seconds. "
                "It keeps going even if you close this tab.")
    _start_worker()
    return "Started. It keeps running even if you close this tab."


def _refresh_seconds() -> str:
    """How often the Queue screen refreshes itself, operator-controlled.

    I had quietly changed this from 3s to 5s while making the fragment do more
    work per tick, which is a UI-feel decision that is not mine to make on
    someone else's machine — the only cost of 3s is more frequent redraws, and
    the only cost of 5s is up to two extra seconds of staleness. So it is a
    setting now, defaulting to the original 3s.
    """
    try:
        from shortforge.providers.store import load_store
        v = float((load_store().get("ui", {}) or {}).get("refresh_seconds") or 3.0)
    except Exception:  # noqa: BLE001
        v = 3.0
    return f"{max(1.0, min(v, 60.0)):g}s"


# --------------------------------------------------------------------------- #
#  📹 Long videos — one list: add links, tick the ones you want, process       #
# --------------------------------------------------------------------------- #

_SHAPES = {"16:9 landscape": "16:9", "9:16 portrait": "9:16", "1:1 square": "1:1"}
_STATUS = {"listed": "⚪ waiting", "pending": "⏳ queued", "running": "▶ working",
           "done": "✅ done", "failed": "✗ failed"}
_DUB = {"Keep the original audio": "captions", "Dub with a synthetic voice": "voice",
        "Dub with a cloned voice (ElevenLabs)": "clone"}
_VIDEO_TYPES = ["mp4", "mov", "mkv", "webm", "m4v"]


def _work_dir() -> str:
    return Config.load().get("paths.work_dir", ".shortforge")


def _flash(kind: str, text: str) -> None:
    st.session_state.setdefault("lv_flash", []).append((kind, text))


def _show_flash() -> None:
    for kind, text in st.session_state.pop("lv_flash", []):
        getattr(st, kind)(text)


def _split_sources(text: str) -> tuple[list[str], list[str]]:
    """Lines that are links or video files on this PC, and lines that are neither."""
    ok, bad = [], []
    for line in (text or "").splitlines():
        s = line.strip().strip('"').strip()
        if not s:
            continue
        if s.lower().startswith(("http://", "https://", "www.")) or os.path.isfile(s):
            ok.append(s)
        else:
            bad.append(s)
    return ok, bad


def _save_upload(work_dir: str, uploaded) -> str:
    folder = os.path.join(work_dir, "uploads")
    os.makedirs(folder, exist_ok=True)
    name = re.sub(r"[^\w.\- ]+", "_", os.path.basename(uploaded.name)) or "upload.mp4"
    path = os.path.abspath(os.path.join(folder, name))
    with open(path, "wb") as f:
        f.write(uploaded.getbuffer())
    return path


def _save_upload_as(work_dir: str, uploaded, prefix: str) -> str:
    path = _save_upload(work_dir, uploaded)
    final = os.path.join(os.path.dirname(path), prefix + os.path.basename(path))
    os.replace(path, final)
    return final


def _new_link_settings() -> tuple[dict, str, object]:
    """The settings panel for the links being added. Returns (settings, label, logo file)."""
    from shortforge.reframe.resolution import CHOICES as _RC
    from shortforge.render.render import QUALITY_CHOICES

    c1, c2, c3, c4 = st.columns(4)
    n_clips = c1.number_input("Clips per video", min_value=0, value=3, step=1, key="lv_clips",
                              help="0 = let ShortForge decide from the video's length.")
    dur = c2.number_input("Clip length (seconds)", min_value=5, value=60, step=5, key="lv_dur",
                          help="No upper limit — 120 or 180 for long clips is fine.")
    shape = c3.selectbox("Shape", list(_SHAPES), 0, key="lv_shape",
                         help="Landscape and square centre-crop. Portrait follows the speaker.")
    res_keys = [k for k, _ in _RC]
    res = c4.selectbox("Resolution", res_keys, res_keys.index("1080p"), key="lv_res",
                       format_func=lambda k: dict(_RC)[k])
    logo = st.checkbox(
        "🧽 Remove logos / watermarks from these videos", key="lv_logo",
        help="Finds anything that stays fixed on screen while the picture changes — a "
             "channel logo, a watermark, a wordmark, even a faint one (down to about 3% "
             "opacity) — and fills it in from the pixels around it, in every clip. Adds "
             "about half a minute per video to look for it. Only for videos you own or "
             "are licensed to edit.")
    with st.expander("More settings (optional)"):
        m1, m2 = st.columns(2)
        language = m1.text_input("Output language", key="lv_lang",
                                 placeholder="blank = keep the original",
                                 help="An ISO code (en, fr, de, es, ja, ar …) to translate the "
                                      "captions — and dub, if you pick a voice.")
        dub = m2.selectbox("Audio", list(_DUB), 0, key="lv_dub",
                           help="Only used with an output language. Dubbing replaces the "
                                "voice and keeps the music/sound-effects bed.")
        m3, m4 = st.columns(2)
        q_keys = [k for k, _ in QUALITY_CHOICES]
        quality = m3.selectbox("Encode quality", q_keys, q_keys.index("high"), key="lv_quality",
                               format_func=lambda k: dict(QUALITY_CHOICES)[k])
        caption = m4.selectbox("Caption style", ["(default)"] + list_templates(), 0,
                               key="lv_caps")
        a1, a2, a3 = st.columns(3)
        animation = a1.selectbox("Caption animation", ["(default)"] + ANIMATIONS, 0,
                                 key="lv_anim")
        camera = a2.selectbox("Camera", ["Centre crop", "Follow the speaker"], 0,
                              key="lv_cam", help="Follow the speaker = virtual camera "
                                                 "(portrait only; slower).")
        encoder = a3.selectbox("Video encoder", ["x264", "auto", "qsv"], 0, key="lv_enc",
                               format_func={"x264": "x264 (software, safe)",
                                            "auto": "auto (best hardware)",
                                            "qsv": "qsv (Intel Quick Sync)"}.get,
                               help="Falls back to x264 by itself if hardware encoding fails.")
        w1, w2, w3 = st.columns(3)
        whisper = w1.selectbox("Transcription accuracy", ["(default)", "small", "medium",
                                                          "large-v3", "base"], 0,
                               key="lv_whisper",
                               help="Whisper model. 'small' is the sensible default on this "
                                    "CPU; medium/large-v3 are slower.")
        src_lang = w2.text_input("Spoken language", key="lv_srclang",
                                 placeholder="blank = detect",
                                 help="Force the language spoken in the video (fr, en …).")
        jumpcuts = w3.checkbox("Trim dead air (jump cuts)", key="lv_jump")
        m5, m6, m7 = st.columns(3)
        metadata = m5.checkbox("Title, description & tags", key="lv_meta",
                               help="Uses your configured AI model once per clip.")
        thumb = m6.checkbox("Cover thumbnail", key="lv_thumb")
        label = m7.text_input("Label (optional)", key="lv_label", placeholder="podcast",
                              help="Tags these videos' output files so you can tell them apart.")
        overlay = st.file_uploader("Add YOUR logo on top of every clip (optional)",
                                   type=["png", "jpg"], key="lv_overlay")
    settings = {"num_clips": int(n_clips) or None, "duration": int(dur),
                "aspect": _SHAPES[shape], "resolution": res, "quality": quality,
                "remove_logos": bool(logo), "metadata": bool(metadata),
                "thumbnail": bool(thumb)}
    if language.strip():
        settings["language"] = language.strip()
        settings["dub"] = _DUB[dub]
    if caption != "(default)":
        settings["caption_template"] = caption
    if animation != "(default)":
        settings["caption_animation"] = animation
    if camera != "Centre crop":
        settings["reframe_mode"] = "track"
    if encoder != "x264":
        settings["encoder"] = encoder
    if whisper != "(default)":
        settings["whisper_model"] = whisper
    if src_lang.strip():
        settings["source_language"] = src_lang.strip()
    if jumpcuts:
        settings["jumpcuts"] = True
    return settings, label, overlay


def _add_section(work_dir: str) -> None:
    from shortforge import queue as Q

    st.subheader("1. Add videos")
    text = st.text_area(
        "Video links — one per line, as many as you like", key=_urls_key(), height=110,
        placeholder="https://www.youtube.com/watch?v=...\nhttps://youtu.be/...\n"
                    "C:\\Videos\\my-video.mp4  (a file on this PC works too)")
    with st.expander("…or upload a video file (up to 200 MB)"):
        up = st.file_uploader("Video file", type=_VIDEO_TYPES, key="lv_upload",
                              label_visibility="collapsed")
    st.markdown("**Settings for these videos** — each video keeps its own; you can still "
                "change them in the list below until it starts.")
    settings, label, overlay = _new_link_settings()
    owner = st.checkbox("I confirm these are my own or licensed videos", key="lv_owner",
                        help="ShortForge only processes content you own or have the rights "
                             "to — this keeps the clips monetizable and free of Content-ID "
                             "claims.")
    sources, bad = _split_sources(text)
    for b in bad[:5]:
        st.warning(f"Not a link, and not a file on this PC: {b[:90]}")
    n = len(sources) + (1 if up is not None else 0)
    if n == 0:
        st.info("➕ Paste a link above (or several, one per line).")
        return
    if not owner:
        st.info("☑️ Tick the ownership confirmation to continue.")
    go = False
    if n == 1:
        b1, b2 = st.columns(2)
        go = b1.button("▶ Process this video now", type="primary", width="stretch",
                       disabled=not owner, key="lv_go")
        add = b2.button("➕ Add to the queue", width="stretch", disabled=not owner,
                        key="lv_add")
    else:
        add = st.button(f"➕ Add {n} videos to the queue", type="primary", width="stretch",
                        disabled=not owner, key="lv_add")
    if not (go or add):
        return

    if up is not None:
        sources.append(_save_upload(work_dir, up))
    if overlay is not None:
        settings["logo_overlay"] = _save_upload_as(work_dir, overlay, "brand_")
    _, (added, skipped) = Q.mutate_queue(
        work_dir, lambda q: Q.add_links(q, sources, settings, label=label, status=Q.LISTED))
    if added and not go:
        _flash("success", f"Added {len(added)} video(s). Tick them in the list below and "
                          f"press ▶ Process — or ▶ Process all.")
    if skipped:
        _flash("warning", f"{len(skipped)} video(s) are already in the list, so they were "
                          f"not added again:\n\n" + Q.describe_skipped(
                              skipped, "A failed one can be retried from the list: tick it "
                                       "and press ▶ Process selected. To redo a finished one, "
                                       "remove it from the list first."))
    if go:
        target = (added or skipped)[0]
        if target["status"] in (Q.LISTED, Q.FAILED):
            _process(work_dir, {target["id"]})
        elif target["status"] == Q.DONE:
            _flash("info", "That video is already done — its clips are in 📚 History. "
                           "Remove it from the list first to make clips from it again.")
        else:
            _flash("info", "That video is already queued or working — see the list below.")
    if added:
        st.session_state["lv_urls_ver"] = st.session_state.get("lv_urls_ver", 0) + 1
    _reset_links_table()
    st.rerun()


def _process(work_dir: str, ids) -> int:
    """Queue the chosen waiting/failed videos (all when ``ids`` is None) and start."""
    from shortforge import queue as Q
    _, started = Q.mutate_queue(work_dir, lambda q: Q.start_jobs(q, ids))
    if started:
        _flash("success", f"▶ Processing {len(started)} video(s). " + _begin_working(work_dir))
    _reset_links_table()
    return len(started)


def _urls_key() -> str:
    """A new key empties the box — clearing the old key's value leaves the text on screen."""
    return f"lv_urls_{st.session_state.get('lv_urls_ver', 0)}"


def _links_table_key() -> str:
    return f"lv_tbl_{st.session_state.get('lv_tbl_ver', 0)}"


def _reset_links_table() -> None:
    """Start the table afresh after its rows changed — pending edits are by position."""
    st.session_state["lv_tbl_ver"] = st.session_state.get("lv_tbl_ver", 0) + 1


def selected_ids(ids: list[str], edited_rows: dict | None) -> list[str]:
    out = []
    for row, changes in (edited_rows or {}).items():
        try:
            if changes.get("✓") and 0 <= int(row) < len(ids):
                out.append(ids[int(row)])
        except (TypeError, ValueError):
            continue
    return out


def apply_link_edits(work_dir: str, ids: list[str], edited_rows: dict | None) -> tuple[int, int]:
    """Save settings changed in the list. Returns (saved, refused because already started)."""
    from shortforge import queue as Q
    cols = {"Clips": "num_clips", "Seconds": "duration", "Shape": "aspect",
            "Resolution": "resolution", "🧽 Logo": "remove_logos"}
    wanted = {}
    for row, changes in (edited_rows or {}).items():
        try:
            jid = ids[int(row)]
        except (IndexError, TypeError, ValueError):
            continue
        fields = {cols[c]: v for c, v in changes.items() if c in cols and v is not None}
        if fields:
            wanted[jid] = fields

    def fn(q):
        saved = refused = 0
        for jid, fields in wanted.items():
            job = Q.get_job(q, jid)
            if job is None:
                continue
            if not Q.editable(job):
                refused += 1
                continue
            s = job.setdefault("settings", {})
            for k, v in fields.items():
                if k == "num_clips":
                    v = max(0, int(v)) or None
                elif k == "duration":
                    v = max(5, int(v))
                elif k == "remove_logos":
                    v = bool(v)
                if v is None:
                    s.pop(k, None)
                else:
                    s[k] = v
            saved += 1
        return saved, refused

    if not wanted:
        return 0, 0
    return Q.mutate_queue(work_dir, fn)[1]


def _on_links_edit(work_dir: str, ids: list[str], tkey: str) -> None:
    edits = (st.session_state.get(tkey) or {}).get("edited_rows")
    _, refused = apply_link_edits(work_dir, ids, {
        r: {c: v for c, v in ch.items() if c != "✓"} for r, ch in (edits or {}).items()})
    if refused:
        _flash("warning", "A video that has already started or finished can't be changed — "
                          "remove it and add it again to redo it with new settings.")
        _reset_links_table()


def _video_name(url: str) -> str:
    return os.path.basename(url) if os.path.isfile(url) else url


def _running_progress(work_dir: str, running_job: dict) -> None:
    from shortforge import lifecycle, runner

    parsed = runner.read_job_log(work_dir)
    lines = [msg for _, msg in parsed]
    warnings = [(lvl, msg) for lvl, msg in parsed if lvl >= logging.WARNING]
    frac, label = _stage_from_lines(lines)
    cur = (lifecycle.read_state(work_dir) or {}).get("current") or {}
    started = cur.get("started")
    elapsed_text = f"  ·  {int(time.time() - started)}s elapsed" if started else ""
    idx, total = cur.get("index"), cur.get("total")
    loc = f"Link {idx}/{total} — " if idx and total else ""
    st.info("⏳ Working… this updates on its own. You can close this tab — it keeps going.")
    st.progress(frac, text=f"{label}{elapsed_text}")
    st.caption(f"Now: {loc}{_video_name(running_job['url'])[:70]}")
    if warnings:
        with st.expander(f"⚠️ {len(warnings)} warning(s) so far", expanded=False):
            _render_warnings(warnings, limit=8)
    st.caption("Live log")
    st.code("\n".join(lines[-18:]) or "Starting…")


@st.fragment(run_every=_refresh_seconds())
def _links_fragment(work_dir: str) -> None:
    """The list, its live status and its buttons — refreshes on its own."""
    from shortforge import queue as Q
    from shortforge.reframe.resolution import CHOICES as _RC

    q = Q.load_queue(work_dir)
    jobs = q.get("jobs", [])
    st.subheader("2. Your videos")
    if not jobs:
        st.caption("Nothing here yet — add videos above.")
        return
    c = Q.counts(q)
    paused = Q.is_paused(q)
    m = st.columns(5)
    m[0].metric("Waiting for you", c[Q.LISTED])
    m[1].metric("Queued", c[Q.PENDING])
    m[2].metric("Working", c[Q.RUNNING])
    m[3].metric("Done", c[Q.DONE])
    m[4].metric("Clips made", Q.total_clips(q))

    running_job = next((j for j in jobs if j["status"] == Q.RUNNING), None)
    if running_job:
        _running_progress(work_dir, running_job)
    elif c[Q.PENDING] and paused:
        st.warning("⏸ Paused — queued videos wait until you press ▶ Resume.")
    elif c[Q.PENDING] and not _worker_is_live(work_dir):
        st.warning("Queued videos are waiting, but nothing is working on them.")
        if st.button("▶ Start working", type="primary", key="lv_kick"):
            _flash("success", _begin_working(work_dir))
            st.rerun()

    ids = [j["id"] for j in jobs]
    rows = [{"✓": False,
             "Status": _STATUS.get(j["status"], j["status"]),
             "Video": _video_name(j["url"]),
             "🧽 Logo": bool(j["settings"].get("remove_logos")),
             "Clips": int(j["settings"].get("num_clips") or 0),
             "Seconds": int(j["settings"].get("duration") or 60),
             "Shape": j["settings"].get("aspect", "16:9"),
             "Resolution": j["settings"].get("resolution", "1080p"),
             "Made": len(j.get("clips") or []),
             "Note": (j.get("error") or Q.describe_settings(j))[:120]} for j in jobs]
    tkey = _links_table_key()
    st.data_editor(
        rows, key=tkey, hide_index=True, width="stretch", num_rows="fixed",
        height=min(520, 38 + 35 * len(rows)),
        disabled=["Status", "Video", "Made", "Note"],
        column_config={
            "✓": st.column_config.CheckboxColumn("✓", width="small",
                                                 help="Tick the videos to process or remove."),
            "Status": st.column_config.TextColumn("Status", width="small"),
            "Video": st.column_config.TextColumn("Video", width="medium"),
            "Made": st.column_config.NumberColumn("Made", width="small"),
            "Clips": st.column_config.NumberColumn("Clips", min_value=0, step=1, format="%d",
                                                   help="0 = automatic"),
            "Seconds": st.column_config.NumberColumn("Seconds", min_value=5, step=5,
                                                     format="%d"),
            "Shape": st.column_config.SelectboxColumn("Shape", options=["16:9", "9:16", "1:1"]),
            "Resolution": st.column_config.SelectboxColumn(
                "Resolution", options=[k for k, _ in _RC]),
            "🧽 Logo": st.column_config.CheckboxColumn(
                "🧽 Logo", width="small",
                help="Remove logos / watermarks from this video's clips."),
        },
        on_change=_on_links_edit, args=(work_dir, ids, tkey))
    st.caption("Tick **✓** the videos you want, then **▶ Process selected** — or "
               "**▶ Process all**. Clips, length, shape, resolution and 🧽 logo removal "
               "can be changed right in the table until a video starts.")

    picked = selected_ids(ids, (st.session_state.get(tkey) or {}).get("edited_rows"))
    startable = {j["id"] for j in jobs if j["status"] in (Q.LISTED, Q.FAILED)}
    sel_start = [i for i in picked if i in startable]
    b1, b2, b3, b4 = st.columns(4)
    if b1.button(f"▶ Process selected ({len(sel_start)})", type="primary", width="stretch",
                 disabled=not sel_start, key="lv_proc_sel"):
        _process(work_dir, set(sel_start))
        st.rerun()
    if b2.button(f"▶ Process all ({len(startable)})", width="stretch",
                 disabled=not startable, key="lv_proc_all"):
        _process(work_dir, None)
        st.rerun()
    if b3.button(f"🗑 Remove selected ({len(picked)})", width="stretch",
                 disabled=not picked, key="lv_rm"):
        _remove(work_dir, set(picked))
        st.rerun()
    if c[Q.PENDING] or c[Q.RUNNING] or paused:
        if b4.button("▶ Resume" if paused else "⏸ Pause after this video", width="stretch",
                     key="lv_pause"):
            Q.mutate_queue(work_dir, lambda qq: Q.set_paused(qq, not paused))
            if paused:
                _flash("success", _begin_working(work_dir))
            st.rerun()
    elif b4.button("🧹 Clear finished", width="stretch", disabled=not c[Q.DONE],
                   key="lv_clear"):
        from shortforge import notify as N
        from shortforge import remote_control as RC
        N.notify(RC._clear(work_dir))
        _reset_links_table()
        st.rerun()

    failed = [j for j in jobs if j["status"] == Q.FAILED]
    if failed:
        st.error(f"✗ **{len(failed)} video(s) failed** — the reason is in the **Note** column "
                 "and on the **🐛 Error logs** screen. Tick them and press ▶ Process selected "
                 "to try again.")
        if any("not a bot" in (j.get("error") or "").lower()
               or "requiring authentication" in (j.get("error") or "") for j in failed):
            st.info("👉 YouTube is asking to sign in: turn off any VPN/proxy, then **⚙️ "
                    "Settings → 📺 YouTube authentication** → pick Firefox → Save.")
    if c[Q.DONE]:
        st.caption("Finished clips are on the **📚 History** screen (and in the `out` folder).")


def _remove(work_dir: str, ids: set) -> None:
    from shortforge import notify as N
    from shortforge import queue as Q
    fresh, gone = Q.mutate_queue(work_dir, lambda q: Q.remove_jobs(q, ids))
    if gone:
        left = Q.counts(fresh)[Q.PENDING] + Q.counts(fresh)[Q.LISTED]
        N.notify(f"🗑 <b>Removed {len(gone)} link(s)</b>\n"
                 + "\n".join(j["url"][:80] for j in gone[:5])
                 + f"\n{left} link(s) still on the list.")
        _flash("success", f"Removed {len(gone)} video(s).")
    if len(gone) < len(ids):
        _flash("warning", "The video that is working right now was kept — pause first, "
                          "then remove it once it finishes.")
    _reset_links_table()


def _render_long_videos() -> None:
    st.header("📹 Long videos → clips")
    st.caption("Add your videos, tick the ones you want, press ▶ Process. Each video keeps "
               "its own settings, and a video already on the list is never added twice.")
    _show_flash()
    work_dir = _work_dir()
    _add_section(work_dir)
    st.divider()
    _links_fragment(work_dir)


# --------------------------------------------------------------------------- #
#  🐛 Error logs                                                               #
# --------------------------------------------------------------------------- #

@st.fragment(run_every=_refresh_seconds())
def _error_log_fragment(work_dir: str, which: str, errors_only: bool) -> None:
    from shortforge import errorlog as E

    entries = E.read(work_dir, limit=500)
    if which != "all":
        entries = [e for e in entries if e.get("source") == which]
    if errors_only:
        entries = [e for e in entries if e.get("level") in ("ERROR", "CRITICAL")]
    if not entries:
        st.success("✓ No problems recorded" + (" for this section." if which != "all" else "."))
        return
    shown = entries[:60]
    st.caption(f"Newest first — showing {len(shown)} of {len(entries)}.")
    for e in shown:
        when = datetime.datetime.fromtimestamp(float(e.get("t") or 0)).strftime("%d %b %H:%M:%S")
        head = f"**{when}** · {E.LABELS.get(e.get('source'), e.get('source'))}"
        body = f"{head}\n\n{e.get('msg', '')}"
        (st.error if e.get("level") in ("ERROR", "CRITICAL") else st.warning)(body)


def _render_error_logs() -> None:
    from shortforge import errorlog as E

    st.header("🐛 Error logs")
    st.caption("Every problem from making clips (📹 Long videos) and downloading Shorts "
               "(📥 Shorts), newest first. It updates by itself while things run.")
    work_dir = _work_dir()
    c1, c2, c3 = st.columns([3, 3, 1])
    which = c1.radio("From", ["all", E.LONG, E.SHORTS],
                     format_func=lambda k: "Everything" if k == "all" else E.LABELS[k],
                     horizontal=True, key="el_which")
    level = c2.radio("Show", ["Errors and warnings", "Errors only"], horizontal=True,
                     key="el_level")
    if c3.button("🗑 Clear", width="stretch", key="el_clear",
                 help="Empties this log. Nothing else is touched."):
        E.clear(work_dir)
        st.rerun()
    _error_log_fragment(work_dir, which, level == "Errors only")


_manifest_cache: dict[str, tuple[tuple, dict | None]] = {}


def _read_manifest(path: str) -> dict | None:
    """A run's manifest, parsed once per change of the file (manifests carry
    every ffmpeg call's timing, so they are not small)."""
    try:
        st_ = os.stat(path)
    except OSError:
        return None
    sig = (st_.st_mtime_ns, st_.st_size)
    hit = _manifest_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = None
    _manifest_cache[path] = (sig, data)
    return data


def _render_history() -> None:
    st.header("📚 History")
    st.caption("Every finished run and its clips — review earlier work without hunting "
               "through the out/ folder.")
    out_dir = os.path.abspath("out")
    # Recursive: runs now write into out/<uploader>/<video_id>/, and a
    # non-recursive glob would have made every past run vanish from this screen
    # the moment that landed. The flat pattern stays so runs from before the
    # per-source folders — and any run with output_template cleared — still show.
    manifests = sorted(
        set(glob.glob(os.path.join(out_dir, _MANIFEST_GLOB))
            + glob.glob(os.path.join(out_dir, "**", _MANIFEST_GLOB), recursive=True)),
        key=os.path.getmtime, reverse=True)
    runs = []
    for mp in manifests:
        manifest = _read_manifest(mp)
        if manifest is None:
            continue
        ts = datetime.datetime.fromtimestamp(os.path.getmtime(mp)).strftime("%Y-%m-%d %H:%M")
        title = (manifest.get("source") or {}).get("title") or os.path.basename(mp)
        runs.append((mp, manifest, f"{ts}  ·  {title}  ·  {len(manifest.get('clips', []))} clip(s)"))
    if not runs:
        st.info("No runs yet. Finished jobs will appear here.")
        return
    # ONE run on screen at a time. This screen used to draw every run ever made,
    # each with a video player and a download button per clip — and both read
    # the whole clip file on every click. Pick a run; the newest is shown first.
    pick = st.selectbox(f"Run ({len(runs)} so far, newest first)", range(len(runs)),
                        format_func=lambda i: runs[i][2], key="hist_pick")
    mp, manifest, _label = runs[pick or 0]
    key = "hist_" + os.path.basename(mp).replace(".", "_")
    _render_results(manifest, out_dir=os.path.dirname(mp), key_prefix=key, video_on_demand=True)


def main() -> None:
    st.set_page_config(page_title="ShortForge", page_icon="🎬", layout="wide")
    _inject_theme()
    setup_logging(False)
    load_env_file(".env")
    from shortforge import errorlog
    errorlog.install(_work_dir(), errorlog.APP)

    st.title("🎬 ShortForge")
    with st.sidebar:
        screen = st.radio(
            "Screen", ["📹 Long videos", "📥 Shorts", "🐛 Error logs", "📚 History",
                       "⚙️ Settings"], index=0,
            help="📹 Long videos turns your videos into clips. 📥 Shorts downloads Shorts "
                 "from the channels you save. 🐛 Error logs shows every problem from both.")
        st.divider()

    if screen == "📥 Shorts":
        from shortforge.ui.shorts_tab import render as render_shorts
        render_shorts(run_every=_refresh_seconds())
    elif screen == "🐛 Error logs":
        _render_error_logs()
    elif screen == "📚 History":
        _render_history()
    elif screen == "⚙️ Settings":
        from shortforge.ui.settings import render as render_settings
        render_settings()
    else:
        _render_long_videos()
    _resource_footer()


if __name__ == "__main__":
    main()
