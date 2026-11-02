"""STEP 1 — hardware encoder selection, args, and automatic x264 fallback."""

import os
import shutil
import subprocess

import pytest

import shortforge.render.render as R
from shortforge.config import Config
from shortforge.reframe import build_filtergraph
from shortforge.render import (available_hw_encoders, resolve_encoder, video_encode_args,
                               render_clip)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def test_default_is_software_x264():
    assert resolve_encoder(Config.load()) == "libx264"     # never a silent hw switch


def test_explicit_choice_falls_back_when_unavailable(monkeypatch):
    monkeypatch.setattr(R, "available_hw_encoders", lambda: [])
    cfg = Config.load()
    cfg.override("render.encoder", "qsv")
    assert resolve_encoder(cfg) == "libx264"               # requested but not present
    cfg.override("render.encoder", "auto")
    assert resolve_encoder(cfg) == "libx264"               # auto with no hw -> software


def test_auto_and_explicit_pick_hardware(monkeypatch):
    monkeypatch.setattr(R, "available_hw_encoders", lambda: ["h264_qsv", "h264_nvenc"])
    cfg = Config.load()
    cfg.override("render.encoder", "auto")
    assert resolve_encoder(cfg) == "h264_qsv"              # best available
    cfg.override("render.encoder", "nvenc")
    assert resolve_encoder(cfg) == "h264_nvenc"


def test_encode_args_shapes():
    c = Config.load()
    x = video_encode_args(c, "libx264")
    assert "libx264" in x and "-crf" in x and "yuv420p" in x
    q = video_encode_args(c, "h264_qsv")
    assert "h264_qsv" in q and "-global_quality" in q
    c.override("render.qsv_quality", 20)
    assert "20" in video_encode_args(c, "h264_qsv")        # quality knob is configurable


@needs_ffmpeg
def test_hardware_encoder_falls_back_to_x264(tmp_path):
    # h264_qsv is compiled into ffmpeg here but there's no Intel GPU, so a real
    # render with encoder=qsv MUST auto-fall back to libx264 and still succeed.
    if "h264_qsv" not in available_hw_encoders():
        pytest.skip("no qsv encoder present to force a failure")
    from shortforge.models import Clip
    src = str(tmp_path / "s.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-pix_fmt", "yuv420p", "-shortest", src],
        check=True, capture_output=True)
    cfg = Config.load()
    cfg.override("render.encoder", "qsv")
    fg = build_filtergraph(1280, 720, 720, 1280, "crop")
    out = str(tmp_path / "o.mp4")
    render_clip(src, Clip(clip_id="01", source_hash="h", start=0.2, end=2.5, score=1.0),
                fg, cfg, out)
    assert os.path.isfile(out) and os.path.getsize(out) > 0   # fallback produced a valid clip
