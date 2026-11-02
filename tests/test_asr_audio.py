"""Whisper gets the audio as samples, never as a path for PyAV to open.

Operator's screen: "Transcription failed via local-whisper: open() got an
unexpected keyword argument 'metadata_errors'". PyAV 19 (Sept 2026) removed
that argument; every faster-whisper release passes it when handed a FILE.
Handed samples, faster-whisper never calls PyAV.
"""

import shutil
import subprocess
import sys
import types
import wave

import numpy as np
import pytest

from shortforge.config import Config
from shortforge.providers import asr as A


def _wav(path, *, rate=16000, channels=1, seconds=0.5):
    n = int(rate * seconds)
    tone = (np.sin(np.arange(n) / rate * 2 * np.pi * 440) * 12000).astype("<i2")
    data = np.repeat(tone, channels)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return str(path)


def test_the_16k_wav_shortforge_makes_is_read_without_ffmpeg_or_pyav(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_SAMPLE_RATE", 16000)
    monkeypatch.setitem(sys.modules, "av", None)               # PyAV unusable
    import shortforge.utils as U
    monkeypatch.setattr(U, "run", lambda *a, **k: pytest.fail("ffmpeg not needed"))
    audio = A.load_audio_16k(_wav(tmp_path / "a.wav"))
    assert audio.dtype == np.float32 and len(audio) == 8000
    assert 0.3 < float(np.abs(audio).max()) < 0.4                # 12000 / 32768


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_other_audio_is_converted_with_ffmpeg_first(tmp_path):
    audio = A.load_audio_16k(_wav(tmp_path / "b.wav", rate=44100, channels=2))
    assert audio.dtype == np.float32 and abs(len(audio) - 8000) < 50


def test_whisper_is_handed_samples_not_a_path(tmp_path, monkeypatch):
    seen = {}

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, audio, **k):
            seen["audio"] = audio
            seg = types.SimpleNamespace(start=0.0, end=0.5, text=" hi ", words=[],
                                        avg_logprob=-0.1, no_speech_prob=0.0)
            return iter([seg]), types.SimpleNamespace(language="en", duration=0.5)

    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=FakeModel))
    t = A.LocalWhisperASR(Config.load()).transcribe(_wav(tmp_path / "c.wav"))
    assert isinstance(seen["audio"], np.ndarray)
    assert [s.text for s in t.segments] == ["hi"]


def test_pyav_is_kept_below_19():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    req = open(os.path.join(root, "requirements.txt"), encoding="utf-8").read()
    assert "av>=11,<19" in req
