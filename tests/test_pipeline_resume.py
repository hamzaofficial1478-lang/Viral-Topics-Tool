"""Clip-resume validity check: existence + non-zero size alone was never
enough. ffmpeg writes straight to the final output path with no
temp-file+rename, so a crash mid-encode (power cut, kill) leaves a real,
non-empty, CORRUPT file sitting exactly where a resumed run looks for a
finished clip — this is the exact scenario an interrupted render leaves
behind, and treating that file as "done" would silently ship a broken clip."""

from shortforge import pipeline as P
from shortforge.utils import MediaInfo


def _fake_probe(duration: float):
    return lambda path: MediaInfo(width=1080, height=1920, duration=duration, fps=30.0,
                                  has_audio=True)


def test_reuse_false_when_resume_is_off(tmp_path, monkeypatch):
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x" * 100)
    monkeypatch.setattr(P, "ffprobe_info", _fake_probe(45.0))
    assert P._clip_is_reusable(str(out), resume=False) is False


def test_reuse_false_when_nothing_exists_yet(tmp_path):
    out = tmp_path / "clip.mp4"
    assert P._clip_is_reusable(str(out), resume=True) is False


def test_reuse_false_for_a_zero_byte_file(tmp_path):
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"")
    assert P._clip_is_reusable(str(out), resume=True) is False


def test_reuse_true_for_a_valid_complete_clip(tmp_path, monkeypatch):
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x" * 5000)
    monkeypatch.setattr(P, "ffprobe_info", _fake_probe(45.0))
    assert P._clip_is_reusable(str(out), resume=True) is True


def test_reuse_false_when_probe_raises_on_a_corrupt_file(tmp_path, monkeypatch):
    """The exact power-cut case: a real, non-empty file that ffprobe can't
    make sense of (truncated mid-write, no moov atom)."""
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"garbage-not-a-real-mp4" * 50)

    def _raise(path):
        raise Exception("moov atom not found")

    monkeypatch.setattr(P, "ffprobe_info", _raise)
    assert P._clip_is_reusable(str(out), resume=True) is False


def test_reuse_false_when_probe_succeeds_but_duration_is_near_zero(tmp_path, monkeypatch):
    """A subtler corruption: ffprobe parses SOME headers but the file was cut
    off almost immediately, so the reported duration is essentially nothing."""
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x" * 2000)
    monkeypatch.setattr(P, "ffprobe_info", _fake_probe(0.2))
    assert P._clip_is_reusable(str(out), resume=True) is False
