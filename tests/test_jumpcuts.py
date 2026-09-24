"""M9 jump-cut wiring: filtergraph select, caption remap, frame-skip helper."""

from shortforge.captions.ass import build_ass
from shortforge.config import Config
from shortforge.models import Clip, Segment, Transcript, Word
from shortforge.render.compose import build_filtergraph
from shortforge.render.render import _in_ranges


def test_filtergraph_injects_video_select():
    fg = build_filtergraph(
        1920, 1080, 1080, 1920, fill="crop",
        video_select="between(t,0.000,2.000)+between(t,7.000,9.000)",
    )
    # A select+PTS-reset node feeds the crop, which now reads from [jc].
    assert "select='between(t,0.000,2.000)+between(t,7.000,9.000)'" in fg
    assert "setpts=N/FRAME_RATE/TB[jc]" in fg
    assert "[jc]crop=" in fg
    assert fg.strip().endswith("[v]")


def test_filtergraph_no_select_unchanged():
    fg = build_filtergraph(1920, 1080, 1080, 1920, fill="crop")
    assert "select=" not in fg
    assert "[0:v]crop=" in fg


def test_filtergraph_select_skipped_when_pre_cropped():
    # Tracked path cuts frames in Python, so no video select goes in the graph.
    fg = build_filtergraph(
        1080, 1920, 1080, 1920, pre_cropped=True,
        video_select="between(t,0.000,2.000)",
    )
    assert "select=" not in fg


def test_in_ranges_membership():
    ranges = [(0.0, 2.0), (7.0, 9.0)]
    assert _in_ranges(0.0, ranges) is True
    assert _in_ranges(1.5, ranges) is True
    assert _in_ranges(4.0, ranges) is False   # in the trimmed gap
    assert _in_ranges(8.0, ranges) is True
    assert _in_ranges(9.5, ranges) is False


def _clip_and_transcript():
    # Speech 0-2s, dead air 2-7s, speech 7-9s (source time).
    words = (
        [Word(0.0 + i * 0.4, 0.4 + i * 0.4, f"a{i}") for i in range(5)]      # 0.0-2.0
        + [Word(7.0 + i * 0.4, 7.4 + i * 0.4, f"b{i}") for i in range(5)]    # 7.0-9.0
    )
    seg = Segment(0.0, 9.0, " ".join(w.text for w in words), words)
    tr = Transcript("en", 9.0, [seg])
    clip = Clip(clip_id="01", source_hash="h", start=0.0, end=9.0, score=1.0)
    return clip, tr


def test_build_ass_remaps_onto_compressed_timeline(tmp_path):
    clip, tr = _clip_and_transcript()
    cfg = Config.load()
    keep = [(0.0, 2.1), (6.9, 9.0)]  # drop the 2.1-6.9 dead air (~4.8s)
    out = build_ass(clip, tr, 1080, 1920, cfg, str(tmp_path / "c.ass"), keep_ranges=keep)
    assert out is not None
    text = open(out, encoding="utf-8").read()

    # The last spoken word ("b4") ends at source 9.0 but the clip is only ~4.2s
    # after trimming, so its caption must land well before source time.
    # No dialogue may start at or beyond 0:00:05 (would mean the gap wasn't cut).
    import re
    starts = re.findall(r"Dialogue: 0,(\d):(\d\d):(\d\d)\.(\d\d),", text)
    max_start = max(int(h) * 3600 + int(m) * 60 + int(s) for h, m, s, _ in starts)
    assert max_start < 5  # compressed: everything fits inside ~4.2s


def test_build_ass_without_keep_ranges_uses_source_timing(tmp_path):
    clip, tr = _clip_and_transcript()
    cfg = Config.load()
    out = build_ass(clip, tr, 1080, 1920, cfg, str(tmp_path / "c.ass"))
    text = open(out, encoding="utf-8").read()
    # Without jump cuts, the later words keep their ~7-9s source timing.
    assert "0:00:07" in text or "0:00:08" in text
