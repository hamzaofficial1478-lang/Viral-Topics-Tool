#!/usr/bin/env python3
"""ShortForge web dashboard (Part D) — a thin Streamlit layer over the pipeline.

Launch:

    streamlit run app.py

It reuses `run_pipeline` (no pipeline logic is duplicated here): fill in a form,
click Run, then watch live per-stage progress + a log tail while the job runs in
a background thread, and when it finishes review each clip inline — video,
thumbnail, hook score + justification, copy-ready title/description/tags, a
download button, the run summary + QC, and a button that opens the output folder.
A History screen lists past runs. The CLI stays fully functional — this is just
an additional entry point.
"""

from __future__ import annotations

import datetime
import glob
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    raise SystemExit("Streamlit is not installed. Run: pip install streamlit")

from shortforge.captions.templates import ANIMATIONS, list_templates
from shortforge.config import Config
from shortforge.pipeline import run_pipeline
from shortforge.research import suggest
from shortforge.utils import ShortForgeError, load_env_file, setup_logging

# Map log-line keywords to a progress fraction + friendly stage label.
_STAGES = [
    ("ingest", 0.10, "Ingesting source"),
    ("transcrib", 0.25, "Transcribing"),
    ("hook scoring", 0.38, "Scoring hooks"),
    ("hook detection", 0.42, "Finding the best moments"),
    ("selected", 0.52, "Selecting clips"),
    ("virtual camera", 0.62, "Reframing (virtual camera)"),
    ("translating", 0.68, "Translating"),
    ("stems", 0.72, "Separating music/voice"),
    ("rendering clip", 0.85, "Rendering"),
    ("wrote manifest", 1.0, "Done"),
]

_MANIFEST_GLOB = "*_manifest.json"


class _QueueHandler(logging.Handler):
    """Push (levelno, message) for each log record onto a queue the UI drains."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put((record.levelno, record.getMessage()))
        except Exception:  # noqa: BLE001
            pass


def _save_upload(uploaded, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(uploaded.getbuffer())
    return path


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


def _plan_steps(duration, tolerance, num, aspect, reframe, whisper_model,
                language, dub_kind) -> list[str]:
    """Plain-language 'what happens next' bullets from the chosen options."""
    lang = (language or "").strip()
    tracking = str(reframe).lower().startswith("track")
    count = "an auto-recommended number of" if int(num) == 0 else f"**{int(num)}**"
    steps = [
        f"**Transcribe** the audio with Whisper **{whisper_model}**.",
        f"**Find the strongest moments** and cut {count} clip(s) of about "
        f"**{int(duration)}s** (±{int(tolerance)}s), ending on natural pauses.",
        f"**Reframe** to **{aspect or '9:16'}** "
        f"({'tracking virtual camera' if tracking else 'center crop'}).",
    ]
    if lang:
        if dub_kind == "captions":
            steps.append(f"**Translate** the captions to **{lang}** (original audio kept).")
        else:
            voice = "a cloned voice" if dub_kind == "clone" else "a synthetic voice"
            steps.append(f"**Translate and dub** into **{lang}** with {voice}, "
                         "separating music/voice so the bed survives.")
    else:
        steps.append("**Keep** the original language and audio.")
    steps.append("**Burn captions, render** vertical MP4s, and write an SEO "
                 "**title, description and tags** for each clip.")
    return steps


def _plan_notes(whisper_model, language, dub_kind) -> list[str]:
    """Honest speed/cost flags to set expectations before a run."""
    lang = (language or "").strip()
    notes = []
    if lang and dub_kind != "captions":
        notes.append("💲 Dubbing calls your TTS provider (ElevenLabs is paid) and the "
                     "translation LLM — the exact cost is shown after transcription.")
    elif lang:
        notes.append("💲 Translation calls your configured LLM provider — cost depends on it.")
    else:
        notes.append("💲 Local-only run — no API cost.")
    if whisper_model in ("medium", "large-v3"):
        notes.append("🐢 High transcription accuracy is slow on this CPU — expect a long "
                     "transcribe stage. 'small' is usually enough.")
    if lang and dub_kind != "captions":
        notes.append("⏳ Dubbing runs Demucs stem separation (~8–12× realtime on CPU) — "
                     "the longest stage; an ETA is logged before it starts.")
    return notes


# --------------------------------------------------------------------------- #
#  Job lifecycle (background thread + session-state so results survive reruns) #
# --------------------------------------------------------------------------- #

def _start_job(source, cfg, transcript_path, out_dir) -> None:
    q: queue.Queue = queue.Queue()
    handler = _QueueHandler(q)
    logging.getLogger("shortforge").addHandler(handler)
    box: dict = {}

    def target():
        try:
            box["manifest"] = run_pipeline(
                source, cfg, owner_confirmed=True, transcript_path=transcript_path)
        except ShortForgeError as e:
            box["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            box["error"] = f"Unexpected error: {e}"

    t = threading.Thread(target=target, daemon=True)
    t.start()
    st.session_state.job = {
        "state": "running", "thread": t, "queue": q, "handler": handler, "box": box,
        "lines": [], "warnings": [], "out_dir": out_dir, "started": time.time(),
    }


def _drain_queue(job: dict) -> None:
    q = job["queue"]
    while True:
        try:
            level, msg = q.get_nowait()
        except queue.Empty:
            break
        job["lines"].append(msg)
        if level >= logging.WARNING:
            job["warnings"].append((level, msg))


def _render_warnings(warnings: list, limit: int | None = None) -> None:
    items = warnings[-limit:] if limit else warnings
    for level, msg in items:
        (st.error if level >= logging.ERROR else st.warning)(msg)


def _render_running(job: dict) -> None:
    _drain_queue(job)
    thread = job["thread"]
    frac, label = _stage_from_lines(job["lines"])
    elapsed = int(time.time() - job["started"])
    st.subheader("⏳ Working on your clips…")
    st.progress(frac, text=f"{label}  ·  {elapsed}s elapsed")
    st.caption("You can leave this page open — it updates live. The job keeps running "
               "even if you switch to Settings.")
    if job["warnings"]:
        with st.expander(f"⚠️ {len(job['warnings'])} warning(s) so far", expanded=False):
            _render_warnings(job["warnings"], limit=8)
    st.caption("Live log")
    st.code("\n".join(job["lines"][-18:]) or "Starting…")

    if thread.is_alive():
        time.sleep(0.5)            # brief poll; the page reruns so it never looks frozen
        st.rerun()
    else:                          # finished — finalize and flip to the result view
        _drain_queue(job)
        logging.getLogger("shortforge").removeHandler(job["handler"])
        if "error" in job["box"]:
            job["state"], job["error"] = "error", job["box"]["error"]
        else:
            job["state"], job["manifest"] = "done", job["box"].get("manifest")
        st.rerun()


def _render_job_result(job: dict) -> None:
    if st.button("◀ Start another job", key="newjob"):
        st.session_state.pop("job", None)
        st.rerun()
    if job["state"] == "error":
        err = job.get("error", "The job failed.")
        st.error(err)
        if "requiring authentication" in err or "not a bot" in err.lower():
            st.info("👉 Open the **Settings** screen (left sidebar) → **📺 YouTube "
                    "authentication**, pick the browser you're logged into YouTube with "
                    "(Firefox is most reliable on Windows), **Save**, then start the job again.")
        if job.get("warnings"):
            st.markdown("**Warnings during the run:**")
            _render_warnings(job["warnings"], limit=10)
        with st.expander("Full log", expanded=False):
            st.code("\n".join(job.get("lines", [])[-60:]) or "(no log)")
        return
    manifest = job.get("manifest")
    if not manifest:
        st.error("No output was produced.")
        return
    if job.get("warnings"):        # item 4: surface stage warnings (e.g. vision 400s)
        with st.expander(f"⚠️ {len(job['warnings'])} warning(s) during the run "
                         "(clips still produced)", expanded=False):
            _render_warnings(job["warnings"])
    _render_results(manifest, out_dir=job.get("out_dir"), key_prefix="live")


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


def _render_clip(c: dict, key: str) -> None:
    vc, ic = st.columns([2, 3])
    fp = c.get("file_path", "")
    with vc:
        if fp and os.path.isfile(fp):
            st.video(fp)
            with open(fp, "rb") as f:
                st.download_button("⬇ Download clip", f.read(), os.path.basename(fp),
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
            with open(pubf, "rb") as f:
                st.download_button("⬇ title/desc/tags (.txt)", f.read(),
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
                    key_prefix: str = "live") -> None:
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
        _render_clip(c, f"{key_prefix}_{i}")


def _render_history() -> None:
    st.header("📚 History")
    st.caption("Every finished run and its clips — review earlier work without hunting "
               "through the out/ folder.")
    out_dir = os.path.abspath("out")
    manifests = sorted(glob.glob(os.path.join(out_dir, _MANIFEST_GLOB)),
                       key=os.path.getmtime, reverse=True)
    if not manifests:
        st.info("No runs yet. Finished jobs will appear here.")
        return
    for mp in manifests:
        try:
            with open(mp, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        src = manifest.get("source", {})
        ts = datetime.datetime.fromtimestamp(os.path.getmtime(mp)).strftime("%Y-%m-%d %H:%M")
        title = src.get("title") or os.path.basename(mp)
        n = len(manifest.get("clips", []))
        with st.expander(f"{ts}  ·  {title}  ·  {n} clip(s)", expanded=False):
            key = "hist_" + os.path.basename(mp).replace(".", "_")
            _render_results(manifest, out_dir=os.path.dirname(mp), key_prefix=key)


# --------------------------------------------------------------------------- #
#  Main                                                                        #
# --------------------------------------------------------------------------- #

def _render_new_job_form() -> None:
    st.caption("Turn your own long-form videos into short vertical clips — captions, "
               "optional dub, SEO titles/tags. Your machine, your content.")

    with st.sidebar:
        st.header("Growing niches")
        if st.button("Suggest niches"):
            for nsug in suggest(top=5):
                st.markdown(f"**{nsug['name']}**  ·  score {nsug['opportunity']}  \n"
                            f"<small>{nsug['why']}</small>", unsafe_allow_html=True)

    # ---- 1. Your video ------------------------------------------------------
    st.subheader("1. Your video")
    source_url = st.text_input(
        "Video URL (from your own channel)",
        help="A link to a video you own or have licensed. Leave blank if uploading a file.")
    up = st.file_uploader("…or upload a file from your computer",
                          type=["mp4", "mov", "mkv", "webm", "m4v"])
    owner = st.checkbox(
        "I confirm this is my own or licensed content",
        value=False,
        help="ShortForge only processes content you own or have the rights to — this keeps "
             "the clips monetizable and free of Content-ID claims.")

    # ---- 2. The basics ------------------------------------------------------
    st.subheader("2. The basics")
    b1, b2, b3 = st.columns(3)
    with b1:
        duration = st.number_input(
            "Clip length (seconds)", min_value=5, value=45, step=5,
            help="How long each short should be. No upper limit — 60, 120, 180s are all fine. "
                 "Clips can never exceed the source video's length.")
    with b2:
        num = st.number_input(
            "How many clips", min_value=0, value=0, step=1,
            help="0 = let ShortForge decide from the video's length and its strongest moments. "
                 "Any explicit number is honoured (limited only by how much good material exists).")
    with b3:
        language = st.text_input(
            "Output language", value="",
            help="Blank = keep the original language. Otherwise an ISO code (en, de, fr, es, "
                 "ja, ar …) to translate the captions — and dub, if you choose a voice below.")

    dub_choices = ["Keep the original audio (captions only)",
                   "Dub with a synthetic voice",
                   "Dub with a cloned voice (via ElevenLabs)"]
    dub_mode = st.selectbox(
        "Audio", dub_choices, 0,
        help="Only applies when you set an output language. Dubbing replaces the original "
             "voice and keeps the music/sound-effects bed. Cloning routes through your "
             "configured ElevenLabs provider (local cloning is impractical on CPU).")
    dub_kind = ("captions", "voice", "clone")[dub_choices.index(dub_mode)]

    # ---- Advanced -----------------------------------------------------------
    with st.expander("Advanced options (defaults are sensible — open only if you need them)"):
        a1, a2 = st.columns(2)
        with a1:
            tolerance = st.number_input(
                "Length tolerance (± seconds)", min_value=1, value=12, step=1,
                help="How far a clip may stray from the target length so it can end on a "
                     "natural pause instead of mid-sentence.")
            aspect = st.text_input(
                "Aspect / shape", value="9:16",
                help="9:16 (vertical), 1:1 (square), 16:9 (wide), or an exact WxH such as "
                     "1080x1920. This is the SHAPE — pixel size is set separately below.")
            resolution = st.selectbox(
                "Resolution (pixel size)", ["1080p", "720p", "480p"], 0,
                help="The short side in pixels — separate from aspect. Lower renders "
                     "meaningfully faster on CPU; 720p is fine for most social destinations.")
            from shortforge.reframe import estimate_export
            try:
                _est = estimate_export(resolution, aspect, float(duration), int(num) or 1)
                st.caption(f"→ {_est['width']}×{_est['height']} · ~{_est['mb_per_clip']} MB/clip "
                           f"· render {_est['render_speed']}")
            except Exception:  # noqa: BLE001
                pass
            reframe = st.selectbox(
                "Reframing", ["Track the action (virtual camera)", "Center crop"], 0,
                help="Track follows the speaker/motion across the frame; center is a fixed crop.")
            whisper_model = st.selectbox(
                "Transcription accuracy", ["small", "base", "medium", "large-v3"], 0,
                help="'small' is the balanced CPU default. 'base' is faster but garbles hard "
                     "speech. 'medium'/'large-v3' are more accurate but much slower on CPU.")
            source_lang = st.text_input(
                "Spoken-language hint (blank = autodetect)", "",
                help="Force the source language (ISO code, e.g. fr) if autodetect misreads it.")
        with a2:
            template = st.selectbox("Caption style", list_templates())
            animation = st.selectbox("Caption animation", ["(style default)"] + ANIMATIONS)
            jumpcuts = st.checkbox(
                "Trim dead air (jump cuts)", value=False,
                help="Cut long silent gaps so the clip feels tighter.")
            enc_choices = ["x264 (software, default)", "auto (best hardware)",
                           "qsv (Intel Quick Sync)"]
            encoder_pick = st.selectbox(
                "Video encoder", enc_choices, 0,
                help="x264 is the safe default. QSV (Intel Quick Sync) is much faster on a "
                     "hardware-encode machine — compare quality first with `encode-sample`. "
                     "Falls back to x264 automatically if hardware encoding fails.")
            encoder = {"x264 (software, default)": "x264", "auto (best hardware)": "auto",
                       "qsv (Intel Quick Sync)": "qsv"}[encoder_pick]
            logo = st.file_uploader("Logo overlay (optional)", type=["png", "jpg"])

    # ---- 3. What happens next ----------------------------------------------
    st.subheader("3. What happens next")
    for step in _plan_steps(duration, tolerance, num, aspect, reframe, whisper_model,
                            language, dub_kind):
        st.markdown(f"- {step}")
    for note in _plan_notes(whisper_model, language, dub_kind):
        st.caption(note)

    # ---- Run (with inline validation) --------------------------------------
    has_source = bool(source_url.strip() or up is not None)
    ready = has_source and owner
    if not has_source:
        st.info("➕ Add a video URL or upload a file to continue.")
    elif not owner:
        st.info("☑️ Tick the ownership confirmation to continue.")
    run = st.button("Run ▶", type="primary", width="stretch", disabled=not ready)
    if not run:
        return

    source = source_url.strip()
    transcript_path = None
    if up is not None:
        source = _save_upload(up, os.path.splitext(up.name)[1] or ".mp4")

    cfg = Config.load()
    cfg.override("reframe.aspect", aspect.strip() or "9:16")
    cfg.override("reframe.resolution", resolution)
    cfg.override("select.target_duration", int(duration))
    cfg.override("select.tolerance", int(tolerance))
    cfg.override("select.num_clips", int(num))
    cfg.override("captions.template", template)
    if animation != "(style default)":
        cfg.override("captions.animation", animation)
    cfg.override("reframe.mode", "center" if reframe.startswith("Center") else "track")
    cfg.override("render.encoder", encoder)
    cfg.override("edit.jumpcuts", bool(jumpcuts))
    cfg.override("transcribe.model", whisper_model)
    if source_lang.strip():
        cfg.override("transcribe.language", source_lang.strip())
    cfg.override("localize.language", language.strip() or None)
    if language.strip() and dub_kind != "captions":
        cfg.override("localize.dub", True)
        cfg.override("localize.tts_backend", "xtts" if dub_kind == "clone" else "auto")
    out_dir = os.path.abspath("out")
    cfg.override("paths.output_dir", out_dir)
    if logo is not None:
        cfg.override("brand.logo", _save_upload(logo, os.path.splitext(logo.name)[1] or ".png"))

    _start_job(source, cfg, transcript_path, out_dir)
    st.rerun()


def main() -> None:
    st.set_page_config(page_title="ShortForge", page_icon="🎬", layout="wide")
    setup_logging(False)
    load_env_file(".env")

    st.title("🎬 ShortForge")

    with st.sidebar:
        screen = st.radio("Screen", ["New job", "Settings", "History"], index=0)
        st.divider()

    if screen == "Settings":
        from shortforge.ui.settings import render as render_settings
        render_settings()
        return
    if screen == "History":
        _render_history()
        return

    # New job — one of three states, all persisted in session_state so results
    # survive the reruns that every button click / copy triggers.
    job = st.session_state.get("job")
    if job and job.get("state") == "running":
        _render_running(job)
        return
    if job and job.get("state") in ("done", "error"):
        _render_job_result(job)
        return
    _render_new_job_form()


if __name__ == "__main__":
    main()
