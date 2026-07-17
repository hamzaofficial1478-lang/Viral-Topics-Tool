"""M4 clip recommender + selection."""

from shortforge.config import Config
from shortforge.detect import detect_hooks
from shortforge.select import build_clips, recommend_clip_count


def _cfg(**over):
    c = Config.load()
    c.override("detect.backend", "heuristic")
    c.override("detect.min_segment_score", 0.0)
    for k, v in over.items():
        c.override(k, v)
    return c


def test_recommend_counts_strong_moments(sample_transcript):
    cfg = _cfg()
    cfg.override("select.target_duration", 20)
    cands = detect_hooks(sample_transcript, cfg)
    n, rationale = recommend_clip_count(sample_transcript, cands, cfg)
    assert n >= 1
    assert "clip" in rationale.lower()


def test_clips_do_not_overlap_and_hit_target(sample_transcript):
    cfg = _cfg()
    cfg.override("select.target_duration", 8)
    cfg.override("select.tolerance", 4)
    cfg.override("select.num_clips", 3)
    cands = detect_hooks(sample_transcript, cfg)
    clips = build_clips(sample_transcript, cands, cfg, "hash")
    assert len(clips) == 3

    # Chronological, non-overlapping.
    for a, b in zip(clips, clips[1:]):
        assert a.end <= b.start + 1e-6

    # Boundaries snap to real segment starts/ends (sentence boundaries).
    starts = {round(s.start, 3) for s in sample_transcript.segments}
    ends = {round(s.end, 3) for s in sample_transcript.segments}
    for c in clips:
        assert round(c.start, 3) in starts
        assert round(c.end, 3) in ends
        assert c.caption_text  # captions carry the spoken text


def test_thought_boundary_helpers():
    from shortforge.models import Segment
    from shortforge.select.select import _is_strong_end, _is_thought_start, _gaps
    segs = [
        Segment(0.0, 2.0, "A clean start here."),
        Segment(2.1, 4.0, "So this continues the idea."),   # continuation, tiny gap
        Segment(5.0, 7.0, "But after a pause it is fine."),  # continuation, 1.0s gap
    ]
    gaps = _gaps(segs)
    assert _is_thought_start(0, segs, gaps, 0.5) is True          # index 0
    assert _is_thought_start(1, segs, gaps, 0.5) is False         # "So" + tiny gap
    assert _is_thought_start(2, segs, gaps, 0.5) is True          # "But" but 1.0s pause
    assert _is_strong_end(1, segs, gaps, 0.5) is True             # 1.0s pause after seg1
    assert _is_strong_end(0, segs, gaps, 0.5) is False            # 0.1s gap


def _story_transcript():
    from shortforge.models import Segment, Transcript, Word

    def seg(s, e, t):
        toks = t.split(); per = (e - s) / max(1, len(toks))
        return Segment(s, e, t, [Word(s + i * per, s + (i + 1) * per, w)
                                 for i, w in enumerate(toks)])
    segs = [
        seg(0.0, 3.0, "Let me set the stage for you today."),
        seg(3.1, 6.0, "So here is the biggest mistake people make."),
        seg(6.1, 9.0, "They give up way too early every time."),
        seg(9.8, 13.0, "Now here is a totally different question."),  # after a pause
        seg(13.1, 16.0, "What if you could double your income today?"),
    ]
    return Transcript("en", 16.0, segs)


def test_coherent_clip_starts_on_a_thought_boundary():
    tr = _story_transcript()
    cfg = _cfg()
    cfg.override("select.target_duration", 8)
    cfg.override("select.tolerance", 4)
    cfg.override("select.num_clips", 1)
    cfg.override("select.coherent", True)
    cands = detect_hooks(tr, cfg)
    clips = build_clips(tr, cands, cfg, "h")
    assert clips
    # The top hook is the "double your income" question; a coherent clip must not
    # begin mid-thought with it — it starts at the thought boundary after the pause.
    assert clips[0].caption_text.startswith("Now here is a totally different question")


def test_top_clip_is_the_strongest_hook(sample_transcript):
    cfg = _cfg()
    cfg.override("select.target_duration", 6)
    cfg.override("select.tolerance", 3)
    cfg.override("select.num_clips", 1)
    cands = detect_hooks(sample_transcript, cfg)
    clips = build_clips(sample_transcript, cands, cfg, "hash")
    assert len(clips) == 1
    assert "Did you know" in clips[0].caption_text
