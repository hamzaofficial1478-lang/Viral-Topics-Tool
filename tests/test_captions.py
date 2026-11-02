"""M7 caption generation."""

import os

from shortforge.captions import build_ass, group_lines, subtitles_filter
from shortforge.captions.ass import _ass_time, _escape
from shortforge.config import Config
from shortforge.models import Clip, Word


def test_ass_time_format():
    assert _ass_time(0) == "0:00:00.00"
    assert _ass_time(65.5) == "0:01:05.50"
    assert _ass_time(3661.5) == "1:01:01.50"
    assert _ass_time(3661.999) == "1:01:02.00"  # centisecond carry


def test_escape_strips_override_braces():
    assert "{" not in _escape("a {b} c")
    assert "}" not in _escape("a {b} c")


def test_group_lines_respects_char_limit():
    words = [Word(i * 0.5, i * 0.5 + 0.5, f"word{i}") for i in range(10)]
    lines = group_lines(words, max_chars=15, max_duration=99)
    assert len(lines) > 1
    for start, end, text in lines:
        assert len(text) <= 15 or " " not in text  # single long word may exceed
        assert end >= start


def test_group_lines_respects_duration():
    words = [Word(i * 2.0, i * 2.0 + 2.0, f"w{i}") for i in range(6)]
    lines = group_lines(words, max_chars=999, max_duration=3.0)
    assert len(lines) > 1


def test_build_ass_writes_file(tmp_path, sample_transcript):
    cfg = Config.load()
    clip = Clip(
        clip_id="01",
        source_hash="h",
        start=sample_transcript.segments[1].start,
        end=sample_transcript.segments[2].end,
        score=0.8,
    )
    out = os.path.join(tmp_path, "clip.ass")
    path = build_ass(clip, sample_transcript, 1080, 1920, cfg, out)
    assert path and os.path.isfile(path)
    body = open(path, encoding="utf-8").read()
    assert "PlayResX: 1080" in body
    assert "PlayResY: 1920" in body
    assert "Dialogue:" in body
    # Timings are relative to the clip (first cue near 0, not the source offset).
    first = body.split("Dialogue: 0,")[1][:10]
    assert first.startswith("0:00:0")


def test_build_ass_disabled_returns_none(sample_transcript, tmp_path):
    cfg = Config.load()
    cfg.override("captions.enabled", False)
    clip = Clip("01", "h", 0.0, 5.0, 0.5)
    assert build_ass(clip, sample_transcript, 1080, 1920, cfg, str(tmp_path / "x.ass")) is None


def test_subtitles_filter_fragment():
    frag = subtitles_filter("/tmp/a b.ass")
    assert frag.startswith("subtitles=filename='")
