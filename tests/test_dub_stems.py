"""A1: stem-separation decision logic + ducking filtergraph (no Demucs needed)."""

import pytest

from shortforge.config import Config
from shortforge.localize import stems
from shortforge.localize.dub import dub_clip
from shortforge.models import Clip, Segment, Transcript
from shortforge.utils import ShortForgeError


def _cfg(**over):
    c = Config.load()
    for k, v in over.items():
        c.override(k, v)
    return c


def test_stem_separation_disabled():
    ok, reason = stems.stem_separation_enabled(_cfg(**{"localize.stem_separation": "false"}))
    assert ok is False and "disabled" in reason


def test_stem_separation_auto_without_demucs(monkeypatch):
    monkeypatch.setattr(stems, "demucs_available", lambda: False)
    ok, reason = stems.stem_separation_enabled(_cfg())
    assert ok is False and "not installed" in reason


def test_stem_separation_auto_with_demucs(monkeypatch):
    monkeypatch.setattr(stems, "demucs_available", lambda: True)
    ok, _ = stems.stem_separation_enabled(_cfg())
    assert ok is True


def test_duck_ratio_deeper_is_stronger():
    assert stems._duck_ratio(-12.0) > stems._duck_ratio(-6.0) > stems._duck_ratio(-3.0)


def test_ducked_filtergraph_has_sidechain_and_params():
    fc = stems.ducked_filtergraph(_cfg(**{
        "localize.duck_db": -6.0,
        "localize.duck_attack_ms": 5,
        "localize.duck_release_ms": 250,
    }))
    assert "sidechaincompress" in fc
    assert "attack=5" in fc and "release=250" in fc
    assert fc.strip().endswith("[out]")
    # voice is split so it both plays and keys the compressor (bed ducks under it)
    assert "asplit=2[voice][key]" in fc


def test_dub_clip_aborts_without_stems_or_bleed(monkeypatch, tmp_path):
    # Force a non-empty voice track so we reach the mix-strategy decision.
    monkeypatch.setattr("shortforge.localize.tts.build_dub_track",
                        lambda *a, **k: str(tmp_path / "voice.wav"))
    (tmp_path / "voice.wav").write_bytes(b"x")
    tr = Transcript("es", 10.0, [Segment(0.0, 3.0, "hola mundo")])
    clip = Clip("01", "h", 0.0, 5.0, 0.9)
    with pytest.raises(ShortForgeError) as e:
        dub_clip("src.mp4", clip, tr, _cfg(), str(tmp_path),
                 accompaniment_source=None, allow_voice_bleed=False)
    assert "original voice" in str(e.value).lower()
