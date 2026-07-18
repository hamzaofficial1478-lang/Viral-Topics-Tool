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


def main() -> None:
    st.set_page_config(page_title="ShortForge", page_icon="🎬", layout="wide")
    setup_logging(False)
    load_env_file(".env")

    st.title("🎬 ShortForge")
    st.caption("Turn your own long-form videos into short vertical clips — captions, "
               "optional dub, SEO titles/tags. Your machine, your content.")

    with st.sidebar:
        st.header("Growing niches")
        if st.button("Suggest niches"):
            for n in suggest(top=5):
                st.markdown(f"**{n['name']}**  ·  score {n['opportunity']}  \n"
                            f"<small>{n['why']}</small>", unsafe_allow_html=True)

    with st.form("run"):
        c1, c2 = st.columns(2)
        with c1:
            source_url = st.text_input("Source video URL (your own channel)")
            up = st.file_uploader("…or upload a local file you own",
                                  type=["mp4", "mov", "mkv", "webm", "m4v"])
            owner = st.checkbox("I confirm this is my own / licensed content", value=False)
            aspect = st.selectbox("Aspect", ["9:16", "1:1", "16:9"], 0)
            duration = st.slider("Target clip length (s)", 15, 90, 45, 5)
            num = st.number_input("Number of clips (0 = recommend)", 0, 20, 0)
        with c2:
            language = st.text_input("Output language (blank = keep source)", "")
            dub_mode = st.selectbox(
                "Audio", ["captions (keep original audio)", "voice (synthetic)",
                          "clone (your voice, xtts)"], 0)
            template = st.selectbox("Caption template", list_templates())
            animation = st.selectbox("Caption animation", ["(template default)"] + ANIMATIONS)
            reframe = st.selectbox("Reframe", ["track (virtual camera)", "center"], 0)
            jumpcuts = st.checkbox("Trim dead air (jump cuts)", value=False)
            logo = st.file_uploader("Logo overlay (optional)", type=["png", "jpg"])
        submitted = st.form_submit_button("Run ▶", use_container_width=True)

    if not submitted:
        return

    source = source_url.strip()
    transcript_path = None
    if up is not None:
        source = _save_upload(up, os.path.splitext(up.name)[1] or ".mp4")
    if not source:
        st.error("Provide a URL or upload a file.")
        return
    if not owner:
        st.error("Ownership confirmation is required — ShortForge only processes your own content.")
        return

    cfg = Config.load()
    cfg.override("reframe.aspect", aspect)
    cfg.override("select.target_duration", int(duration))
    cfg.override("select.num_clips", int(num))
    cfg.override("captions.template", template)
    if animation != "(template default)":
        cfg.override("captions.animation", animation)
    cfg.override("reframe.mode", "center" if reframe.startswith("center") else "track")
    cfg.override("edit.jumpcuts", bool(jumpcuts))
    cfg.override("localize.language", language.strip() or None)
    if language.strip() and not dub_mode.startswith("captions"):
        cfg.override("localize.dub", True)
        cfg.override("localize.tts_backend", "xtts" if dub_mode.startswith("clone") else "auto")
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
