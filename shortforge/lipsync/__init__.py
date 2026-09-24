"""Lip-sync (Phase 3+, optional) — align on-screen lips to a new voiceover.

This only matters for **cross-language dubs**. When ShortForge synthesizes a new
voiceover in another language, the speaker's real lip movements no longer match
the words being heard. Wav2Lip re-renders just the mouth region so the lips
track the dubbed audio. Same-language clips keep the original audio *and* the
real lips, so lip-sync is skipped for them entirely — there is nothing to fix.

Wav2Lip is **not** a pip dependency and wants a GPU for reasonable speed. To use
it, clone https://github.com/Rudrabha/Wav2Lip and set:

    lipsync:
      enabled: true
      wav2lip_repo: /path/to/Wav2Lip          # dir containing inference.py
      checkpoint:   /path/to/wav2lip_gan.pth   # the model weights

If it is missing or fails at runtime, the clip keeps its normal (non-lip-synced)
render and the pipeline continues — graceful degradation, never a hard failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from ..config import Config
from ..utils import log


def _python_exe(cfg: Config) -> str:
    """Interpreter to run Wav2Lip with (it may live in its own venv)."""
    return cfg.get("lipsync.python") or sys.executable


def available(cfg: Config) -> tuple[bool, str]:
    """Return (usable, reason). Reason explains the 'no' case for logs."""
    if not cfg.get("lipsync.enabled", False):
        return False, "disabled"
    backend = cfg.get("lipsync.backend", "auto")
    if backend not in ("auto", "wav2lip"):
        return False, f"unknown backend '{backend}'"

    repo = cfg.get("lipsync.wav2lip_repo")
    ckpt = cfg.get("lipsync.checkpoint")
    if not repo:
        return False, "lipsync.wav2lip_repo not set (clone Rudrabha/Wav2Lip)"
    inference = os.path.join(repo, "inference.py")
    if not os.path.isfile(inference):
        return False, f"inference.py not found in {repo}"
    if not ckpt or not os.path.isfile(ckpt):
        return False, f"checkpoint not found: {ckpt}"
    return True, "wav2lip"


def lipsync_clip(
    video_path: str,
    audio_path: str,
    cfg: Config,
    out_path: str | None = None,
) -> bool:
    """Lip-sync ``video_path`` to ``audio_path`` (the dubbed voiceover).

    Writes the result to ``out_path`` (defaults to overwriting ``video_path``).
    Returns True only if the lip-synced file was produced; on any problem it
    leaves the original render untouched and returns False.
    """
    ok, reason = available(cfg)
    if not ok:
        log.info("lip-sync skipped (%s); keeping original render", reason)
        return False
    if not audio_path or not os.path.isfile(audio_path):
        log.info("lip-sync skipped (no dubbed audio track)")
        return False

    out_path = out_path or video_path
    repo = cfg.get("lipsync.wav2lip_repo")
    ckpt = cfg.get("lipsync.checkpoint")
    pads = str(cfg.get("lipsync.pads", "0 10 0 0")).split()
    resize_factor = str(int(cfg.get("lipsync.resize_factor", 1)))

    fd, tmp_out = tempfile.mkstemp(suffix=".mp4", dir=os.path.dirname(out_path) or ".")
    os.close(fd)

    cmd = [
        _python_exe(cfg),
        os.path.join(repo, "inference.py"),
        "--checkpoint_path", ckpt,
        "--face", os.path.abspath(video_path),
        "--audio", os.path.abspath(audio_path),
        "--outfile", os.path.abspath(tmp_out),
        "--resize_factor", resize_factor,
        "--pads", *pads,
    ]
    if cfg.get("lipsync.nosmooth", False):
        cmd.append("--nosmooth")

    log.info("lip-syncing clip to dubbed audio (Wav2Lip; this can be slow on CPU)")
    try:
        proc = subprocess.run(
            cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except OSError as e:
        log.warning("lip-sync could not start (%s); keeping original render", e)
        _cleanup(tmp_out)
        return False

    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        log.warning("lip-sync failed, keeping original render:\n%s", tail)
        _cleanup(tmp_out)
        return False
    if not os.path.isfile(tmp_out) or os.path.getsize(tmp_out) == 0:
        log.warning("lip-sync produced no output; keeping original render")
        _cleanup(tmp_out)
        return False

    os.replace(tmp_out, out_path)
    log.info("lip-sync applied")
    return True


def _cleanup(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


__all__ = ["available", "lipsync_clip"]
