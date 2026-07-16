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


def test_top_clip_is_the_strongest_hook(sample_transcript):
    cfg = _cfg()
    cfg.override("select.target_duration", 6)
    cfg.override("select.tolerance", 3)
    cfg.override("select.num_clips", 1)
    cands = detect_hooks(sample_transcript, cfg)
    clips = build_clips(sample_transcript, cands, cfg, "hash")
    assert len(clips) == 1
    assert "Did you know" in clips[0].caption_text
