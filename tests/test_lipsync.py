"""Lip-sync toggle: availability gating + graceful degradation."""

import os

from shortforge.config import Config
from shortforge.lipsync import available, lipsync_clip


def _cfg(**over):
    c = Config.load()
    for k, v in over.items():
        c.override(k, v)
    return c


def test_disabled_by_default():
    ok, reason = available(_cfg())
    assert ok is False
    assert reason == "disabled"


def test_enabled_but_repo_missing():
    ok, reason = available(_cfg(**{"lipsync.enabled": True}))
    assert ok is False
    assert "wav2lip_repo" in reason


def test_enabled_repo_without_inference(tmp_path):
    repo = tmp_path / "Wav2Lip"
    repo.mkdir()
    ok, reason = available(_cfg(**{
        "lipsync.enabled": True,
        "lipsync.wav2lip_repo": str(repo),
    }))
    assert ok is False
    assert "inference.py" in reason


def test_enabled_missing_checkpoint(tmp_path):
    repo = tmp_path / "Wav2Lip"
    repo.mkdir()
    (repo / "inference.py").write_text("# stub\n")
    ok, reason = available(_cfg(**{
        "lipsync.enabled": True,
        "lipsync.wav2lip_repo": str(repo),
        "lipsync.checkpoint": str(tmp_path / "missing.pth"),
    }))
    assert ok is False
    assert "checkpoint" in reason


def test_available_when_repo_and_checkpoint_present(tmp_path):
    repo = tmp_path / "Wav2Lip"
    repo.mkdir()
    (repo / "inference.py").write_text("# stub\n")
    ckpt = tmp_path / "wav2lip_gan.pth"
    ckpt.write_bytes(b"\x00")
    ok, reason = available(_cfg(**{
        "lipsync.enabled": True,
        "lipsync.wav2lip_repo": str(repo),
        "lipsync.checkpoint": str(ckpt),
    }))
    assert ok is True
    assert reason == "wav2lip"


def test_lipsync_clip_degrades_when_unavailable(tmp_path):
    # Disabled -> returns False, never touches the file.
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"original-bytes")
    audio = tmp_path / "dub.wav"
    audio.write_bytes(b"audio")
    result = lipsync_clip(str(video), str(audio), _cfg(), str(video))
    assert result is False
    assert video.read_bytes() == b"original-bytes"  # untouched


def test_lipsync_clip_skips_without_audio(tmp_path):
    repo = tmp_path / "Wav2Lip"
    repo.mkdir()
    (repo / "inference.py").write_text("# stub\n")
    ckpt = tmp_path / "wav2lip_gan.pth"
    ckpt.write_bytes(b"\x00")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    cfg = _cfg(**{
        "lipsync.enabled": True,
        "lipsync.wav2lip_repo": str(repo),
        "lipsync.checkpoint": str(ckpt),
    })
    # Enabled + available, but the audio file does not exist -> skip, no crash.
    assert lipsync_clip(str(video), str(tmp_path / "nope.wav"), cfg, str(video)) is False
