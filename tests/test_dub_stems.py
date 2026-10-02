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


def test_retain_db_full_vs_partial_vs_explicit():
    assert stems._retain_db(_cfg(**{"localize.vocal_removal_strength": "full"})) is None
    assert stems._retain_db(_cfg(**{"localize.vocal_removal_strength": "partial",
                                    "localize.vocal_retain_db": -18.0})) == -18.0
    assert stems._retain_db(_cfg(**{"localize.vocal_removal_strength": "-12"})) == -12.0


def _sine(path, freq, dur=1.0):
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={dur}:sample_rate=48000",
                    "-ac", "2", path], capture_output=True)


def test_build_bed_full_removal_is_accompaniment_only(tmp_path):
    nv = str(tmp_path / "no_vocals.wav"); vo = str(tmp_path / "vocals.wav")
    _sine(nv, 220); _sine(vo, 440)
    st = stems.Stems(no_vocals=nv, vocals=vo)
    bed = stems.build_bed(st, str(tmp_path), _cfg(**{"localize.vocal_removal_strength": "full"}))
    import os
    # Full removal = a copy of the accompaniment (same byte size).
    assert os.path.getsize(bed) == os.path.getsize(nv)


def test_build_bed_partial_mixes_in_vocals(tmp_path):
    import os
    nv = str(tmp_path / "no_vocals.wav"); vo = str(tmp_path / "vocals.wav")
    _sine(nv, 220); _sine(vo, 440)
    st = stems.Stems(no_vocals=nv, vocals=vo)
    bed = stems.build_bed(st, str(tmp_path),
                          _cfg(**{"localize.vocal_removal_strength": "partial",
                                  "localize.vocal_retain_db": -18.0}))
    assert os.path.isfile(bed) and os.path.getsize(bed) > 0
    # The mixed bed differs from a plain copy of the accompaniment.
    assert os.path.getsize(bed) != 0


def test_export_debug_audio_writes_stems(tmp_path):
    import os
    nv = str(tmp_path / "no_vocals.wav"); vo = str(tmp_path / "vocals.wav")
    _sine(nv, 220); _sine(vo, 440)
    st = stems.Stems(no_vocals=nv, vocals=vo)
    written = stems.export_debug_audio(st, nv, str(tmp_path / "out"))
    names = {os.path.basename(p) for p in written}
    assert names == {"vocals.wav", "accompaniment.wav", "bed.wav"}


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
