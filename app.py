#!/usr/bin/env python3
"""ShortForge web dashboard (Part D) — a thin Streamlit layer over the pipeline.

Launch:

    streamlit run app.py

It reuses `run_pipeline` (no pipeline logic is duplicated here): you fill in a
form, click Run, watch live per-stage progress, then preview each clip inline
with its title/description/tags and download buttons. The CLI stays fully
functional — this is just an additional entry point.
"""

from __future__ import annotations

import logging
import os
import queue
import tempfile
import threading

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
    ("hook detection", 0.40, "Finding the best moments"),
    ("selected", 0.52, "Selecting clips"),
    ("virtual camera", 0.62, "Reframing (virtual camera)"),
    ("translating", 0.68, "Translating"),
    ("stems", 0.72, "Separating music/voice"),
    ("rendering clip", 0.85, "Rendering"),
    ("wrote manifest", 1.0, "Done"),
]


class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(record.getMessage())
        except Exception:  # noqa: BLE001
            pass


def _run_in_thread(source, cfg, transcript_path):
    """Run the pipeline in a background thread, returning (thread, box)."""
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
    return t, box


def _save_upload(uploaded, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(uploaded.getbuffer())
    return path


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


def main() -> None:
    st.set_page_config(page_title="ShortForge", page_icon="🎬", layout="wide")
    setup_logging(False)
    load_env_file(".env")

    st.title("🎬 ShortForge")

    with st.sidebar:
        screen = st.radio("Screen", ["New job", "Settings"], index=0)
        st.divider()

    if screen == "Settings":
        from shortforge.ui.settings import render as render_settings
        render_settings()
        return

    st.caption("Turn your own long-form videos into short vertical clips — captions, "
               "optional dub, SEO titles/tags. Your machine, your content.")

    with st.sidebar:
        st.header("Growing niches")
        if st.button("Suggest niches"):
            for n in suggest(top=5):
                st.markdown(f"**{n['name']}**  ·  score {n['opportunity']}  \n"
                            f"<small>{n['why']}</small>", unsafe_allow_html=True)

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
    run = st.button("Run ▶", type="primary", use_container_width=True, disabled=not ready)
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

    q: queue.Queue = queue.Queue()
    handler = _QueueHandler(q)
    logging.getLogger("shortforge").addHandler(handler)
    try:
        progress = st.progress(0.0, text="Starting…")
        log_area = st.expander("Live log", expanded=True).empty()
        lines: list[str] = []
        thread, box = _run_in_thread(source, cfg, transcript_path)

        while thread.is_alive() or not q.empty():
            try:
                msg = q.get(timeout=0.2)
                lines.append(msg)
                frac, label = 0.05, "Working…"
                for key, f, lab in _STAGES:
                    if any(key in ln.lower() for ln in lines[-12:]):
                        frac, label = f, lab
                progress.progress(frac, text=label)
                log_area.code("\n".join(lines[-16:]))
            except queue.Empty:
                pass
        thread.join()
    finally:
        logging.getLogger("shortforge").removeHandler(handler)

    if "error" in box:
        st.error(box["error"])
        return
    manifest = box.get("manifest")
    if not manifest:
        st.error("No output was produced.")
        return

    progress.progress(1.0, text="Done")
    st.success(manifest.get("summary", "Done"))
    rec = manifest.get("recommendation", {})
    if rec:
        st.info(rec.get("rationale", ""))

    for c in manifest["clips"]:
        st.divider()
        vc, ic = st.columns([2, 3])
        with vc:
            if os.path.isfile(c["file_path"]):
                st.video(c["file_path"])
                with open(c["file_path"], "rb") as f:
                    st.download_button("⬇ Download clip", f, os.path.basename(c["file_path"]),
                                       mime="video/mp4", key=c["clip_id"])
        with ic:
            md = c.get("metadata") or {}
            st.subheader(md.get("title") or f"Clip {c['clip_id']}")
            st.caption(f"[{c['start']:.1f}–{c['end']:.1f}]  score {c['score']:.2f}"
                       + ("  · pending review" if c.get("review") else ""))
            if md.get("description"):
                st.write(md["description"])
            if md.get("tags"):
                st.markdown("**Tags:** " + ", ".join(md["tags"]))
            if md.get("hashtags"):
                st.markdown("**Hashtags:** " + " ".join(md["hashtags"]))
            if c.get("publish_file") and os.path.isfile(c["publish_file"]):
                with open(c["publish_file"], "rb") as f:
                    st.download_button("⬇ Title/desc/tags (.txt)", f,
                                       os.path.basename(c["publish_file"]),
                                       key="sc" + c["clip_id"])


if __name__ == "__main__":
    main()
