"""Sources with no speech still produce clips.

Operator: a music/no-narration video was refused outright — "this source has no
detectable speech ... try a source with clear speech". Cutting a silent video
into clips of a given length is a complete instruction on its own, so refusing
it was wrong.
"""

import pytest

from shortforge.config import Config
from shortforge.models import Segment, Transcript
from shortforge.select import nospeech as NS
from shortforge.utils import ShortForgeError


class _Meta:
    def __init__(self, duration, path="/tmp/x.mp4"):
        self.duration = duration
        self.file_path = path
        self.hash = "h"


def _cfg(**over):
    c = Config.load()
    for k, v in over.items():
        c.override(k, v)
    return c


# --- detecting "no speech" --------------------------------------------------- #

def test_empty_transcript_is_no_speech():
    assert NS.has_speech(Transcript(language="en", duration=60, segments=[])) is False
    assert NS.has_speech(None) is False


def test_a_wordless_transcript_is_also_no_speech():
    """ASR sometimes returns segments whose text is blank — that is still silence,
    and treating it as speech gives clips anchored on nothing."""
    t = Transcript(language="en", duration=60,
                   segments=[Segment(start=0, end=5, text="   "),
                             Segment(start=5, end=9, text="")])
    assert NS.has_speech(t) is False


def test_real_speech_is_left_alone():
    t = Transcript(language="en", duration=60,
                   segments=[Segment(start=0, end=5, text="hello there")])
    assert NS.has_speech(t) is True


# --- even spacing (no audio to rank on) -------------------------------------- #

def test_clips_are_spread_across_the_whole_source_not_just_the_start():
    """5 clips from a 30-min silent video must sample the whole video, not only
    its first 5 minutes."""
    spans = NS.plan_spans(duration=1800, target=60, n=5, energy=None)
    assert len(spans) == 5
    assert all(round(e - s) == 60 for s, e, _ in spans)
    assert spans[0][0] == 0.0
    assert spans[-1][1] == pytest.approx(1800, abs=1)      # reaches the end
    starts = [s for s, _, _ in spans]
    assert starts == sorted(starts)


def test_spans_never_overlap():
    spans = NS.plan_spans(duration=600, target=90, n=6, energy=None)
    for (a_s, a_e, _), (b_s, _b_e, _) in zip(spans, spans[1:]):
        assert b_s >= a_e - 0.001


def test_a_single_clip_is_taken_from_the_middle():
    (start, end, _), = NS.plan_spans(duration=300, target=60, n=1, energy=None)
    assert start == pytest.approx(120) and end == pytest.approx(180)


def test_span_never_runs_past_the_end_of_the_source():
    for n in (1, 2, 3, 7):
        for s, e, _ in NS.plan_spans(duration=100, target=30, n=n, energy=None):
            assert 0 <= s < e <= 100


# --- loudness ranking (audio present) ---------------------------------------- #

def test_loud_moments_win_when_the_source_has_audio():
    """A music video's chorus should beat its quiet intro."""
    energy = [0.01] * 300
    for i in range(200, 230):          # a loud 30s stretch at 200s
        energy[i] = 0.9
    spans = NS.plan_spans(duration=300, target=30, n=1, energy=energy)
    start, end, score = spans[0]
    assert 195 <= start <= 205
    assert score == pytest.approx(1.0, abs=0.01)
    assert end - start == pytest.approx(30)


def test_ranked_spans_still_never_overlap():
    energy = [0.5] * 600               # flat: every window scores the same
    spans = NS.plan_spans(duration=600, target=60, n=5, energy=energy)
    assert len(spans) == 5
    for (a_s, a_e, _), (b_s, _b, _) in zip(spans, spans[1:]):
        assert b_s >= a_e - 0.001


def test_ranked_spans_come_back_in_playing_order():
    energy = [0.1] * 400
    for i in range(300, 340):
        energy[i] = 1.0                # the best window is late in the video
    for i in range(20, 60):
        energy[i] = 0.8                # the second best is early
    spans = NS.plan_spans(duration=400, target=40, n=2, energy=energy)
    assert [round(s) for s, _, _ in spans] == sorted(round(s) for s, _, _ in spans)


# --- building clips ---------------------------------------------------------- #

def test_build_clips_honours_the_requested_count_and_duration(monkeypatch):
    monkeypatch.setattr(NS, "audio_energy", lambda *a, **k: None)
    clips, basis = NS.build_clips(_Meta(600), _cfg(**{"select.num_clips": 5,
                                                      "select.target_duration": 60}), "hash")
    assert basis == "evenly spaced"
    assert len(clips) == 5
    assert all(c.duration == pytest.approx(60) for c in clips)
    assert [c.clip_id for c in clips] == ["01", "02", "03", "04", "05"]
    assert all(c.caption_text == "" for c in clips)        # nothing was said
    assert all("no speech" in c.reason for c in clips)     # provenance on every clip


def test_asking_for_more_clips_than_fit_is_reported_not_silently_trimmed(
        monkeypatch, caplog):
    monkeypatch.setattr(NS, "audio_energy", lambda *a, **k: None)
    with caplog.at_level("WARNING"):
        clips, _ = NS.build_clips(_Meta(120), _cfg(**{"select.num_clips": 10,
                                                      "select.target_duration": 60}), "h")
    assert len(clips) == 2                                  # 120s / 60s
    assert "only fits 2" in caplog.text and "10 clip" in caplog.text


def test_a_source_shorter_than_the_target_yields_one_whole_clip(monkeypatch, caplog):
    monkeypatch.setattr(NS, "audio_energy", lambda *a, **k: None)
    with caplog.at_level("WARNING"):
        clips, _ = NS.build_clips(_Meta(25), _cfg(**{"select.num_clips": 3,
                                                     "select.target_duration": 60}), "h")
    assert len(clips) == 1 and clips[0].duration == pytest.approx(25)
    assert "shorter than the requested" in caplog.text      # never padded silently


def test_a_truncated_download_is_refused_rather_than_clipped(monkeypatch):
    monkeypatch.setattr(NS, "audio_energy", lambda *a, **k: None)
    with pytest.raises(ShortForgeError) as ei:
        NS.build_clips(_Meta(1.2), _cfg(**{"select.target_duration": 60}), "h")
    assert "nothing to cut" in str(ei.value)
    assert "partial download" in str(ei.value)


def test_the_basis_is_reported_so_a_manifest_cannot_hide_it(monkeypatch):
    monkeypatch.setattr(NS, "audio_energy", lambda *a, **k: [0.2] * 600)
    _clips, basis = NS.build_clips(_Meta(600), _cfg(**{"select.num_clips": 2,
                                                       "select.target_duration": 30}), "h")
    assert basis == "loudest moments"


# --- audio_energy is defensive ----------------------------------------------- #

def test_audio_energy_returns_none_when_ffmpeg_finds_no_audio(monkeypatch):
    """A video with no audio stream must fall back to even spacing, not crash."""
    import subprocess

    class _P:
        returncode = 1
        stdout = b""
        stderr = b"Output file #0 does not contain any stream"

    monkeypatch.setattr(NS, "require_binary", lambda n: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    assert NS.audio_energy("/tmp/silent.mp4", 60) is None


def test_a_silent_audio_track_counts_as_no_audio(monkeypatch):
    """Many "silent" videos carry an all-zero audio track; ranking on it would
    just pick the first window every time."""
    import subprocess

    class _P:
        returncode = 0
        stdout = b"\x00\x00" * (8000 * 5)
        stderr = b""

    monkeypatch.setattr(NS, "require_binary", lambda n: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    assert NS.audio_energy("/tmp/silent.mp4", 5) is None


def test_audio_energy_survives_a_broken_ffmpeg(monkeypatch):
    import subprocess

    monkeypatch.setattr(NS, "require_binary", lambda n: "ffmpeg")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert NS.audio_energy("/tmp/x.mp4", 60) is None       # falls back, never raises


def test_audio_energy_measures_loudness_per_slot(monkeypatch):
    """Quiet first second, loud second second."""
    import struct
    import subprocess

    quiet = struct.pack("<h", 100) * 8000
    loud = struct.pack("<h", 20000) * 8000

    class _P:
        returncode = 0
        stdout = quiet + loud
        stderr = b""

    monkeypatch.setattr(NS, "require_binary", lambda n: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    energy = NS.audio_energy("/tmp/x.mp4", 2)
    assert energy is not None and len(energy) == 2
    assert energy[1] > energy[0] * 10


# --- the two places that used to refuse a silent source ---------------------- #

def test_a_video_with_no_audio_track_is_ingested_not_rejected(tmp_path, monkeypatch):
    """Ingest used to raise "has no audio stream — cannot transcribe or clip on
    speech". A drone reel has no audio track and is still a valid source."""
    import importlib
    I = importlib.import_module("shortforge.ingest.ingest")

    f = tmp_path / "drone.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(I, "ffprobe_info",
                        lambda p: type("M", (), {"duration": 90.0, "has_audio": False,
                                                 "width": 1920, "height": 1080})())
    monkeypatch.setattr(I, "content_key", lambda p: "hash")

    meta = I._ingest_local(str(f))
    assert meta.duration == 90.0 and meta.title == "drone"


def test_transcribe_skips_asr_entirely_when_there_is_no_audio(tmp_path, monkeypatch):
    """No audio means no wav to extract and nothing to transcribe — the run must
    not need Whisper installed just to discover that."""
    import importlib

    from shortforge.cache import Cache

    # shortforge.analyze re-exports the transcribe *function*, which shadows the
    # submodule of the same name — import the module explicitly.
    T = importlib.import_module("shortforge.analyze.transcribe")

    monkeypatch.setattr(T, "ffprobe_info",
                        lambda p: type("M", (), {"duration": 60.0, "has_audio": False,
                                                 "width": 640, "height": 360})())

    def _boom(*a, **k):
        raise AssertionError("extract_audio must not run for a silent source")

    monkeypatch.setattr(T, "extract_audio", _boom)

    meta = type("Meta", (), {"file_path": str(tmp_path / "x.mp4"), "duration": 60.0})()
    tr = T.transcribe(meta, _cfg(), Cache(str(tmp_path), "h"))
    assert tr.segments == [] and tr.duration == 60.0
    assert NS.has_speech(tr) is False
