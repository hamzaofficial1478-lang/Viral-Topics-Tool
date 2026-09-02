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


def test_explicit_clip_count_is_not_capped_at_20():
    """STEP 3.6: an explicit num_clips is honoured (no silent cap) — never degrade."""
    from shortforge.models import Segment, Transcript
    segs, t = [], 0.0
    for i in range(25):                                   # 25 isolated 7s thoughts
        segs.append(Segment(t, t + 7.0, f"Complete standalone thought number {i} here."))
        t += 8.0                                          # 1s pause between each
    tr = Transcript("en", t, segs)
    cfg = _cfg(**{"select.target_duration": 7, "select.tolerance": 1, "select.num_clips": 22})
    clips = build_clips(tr, [], cfg, "hash")
    assert len(clips) == 22                               # > old hardcoded ceiling of 20


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


# --- Blocker 2: clips, not fragments ---------------------------------------- #

def test_overlapping_hook_anchors_collapse_into_one_clip():
    """Three adjacent high-scoring segments (191.8-192.8, 192.8-193.5, 193.5-194.8)
    are the SAME moment — they must collapse into a single clip, not three."""
    from shortforge.models import Segment, Transcript, Candidate
    # A run of continuous speech with three neighbouring strong anchors mid-way.
    segs = [Segment(i * 1.0, i * 1.0 + 1.0, f"and then part {i} of the same idea continues.")
            for i in range(40)]
    tr = Transcript("en", 40.0, segs)
    cands = [Candidate(s.start, s.end, 0.9 if i in (20, 21, 22) else 0.1, "hook")
             for i, s in enumerate(segs)]
    cfg = _cfg()
    cfg.override("select.target_duration", 15)
    cfg.override("select.tolerance", 5)
    cfg.override("select.num_clips", 3)          # ask for 3 — the moment is only one
    clips = build_clips(tr, cands, cfg, "h")
    # The three overlapping anchors fall inside one grown window → one clip covers
    # all three; they do not each spawn a clip of the same moment.
    covering = [c for c in clips if c.start <= 20.0 and c.end >= 23.0]
    assert len(covering) == 1


def test_warns_when_requested_duration_exceeds_remaining_source(caplog):
    """Long-clip guard: a hook near the end can't reach the requested duration —
    warn clearly instead of silently truncating."""
    import logging
    from shortforge.models import Segment, Transcript, Candidate
    # 12s of speech; the only strong hook is the last 3s → a 45s clip is impossible.
    segs = [Segment(i * 3.0, i * 3.0 + 3.0, f"thought number {i} here.") for i in range(4)]
    tr = Transcript("en", 12.0, segs)
    cands = [Candidate(s.start, s.end, 0.9 if i == 3 else 0.1, "hook")
             for i, s in enumerate(segs)]
    cfg = _cfg()
    cfg.override("select.target_duration", 45)
    cfg.override("select.tolerance", 5)
    cfg.override("select.num_clips", 1)
    with caplog.at_level(logging.WARNING, logger="shortforge"):
        clips = build_clips(tr, cands, cfg, "h")
    assert clips                                              # a shorter clip is still emitted
    assert (clips[0].end - clips[0].start) < 45              # shorter than requested
    assert any("short of the requested" in r.message for r in caplog.records)


def test_never_emits_a_sub_minimum_clip():
    """Even with a tiny target + generous tolerance, no clip is shorter than the
    minimum (never a 0.7s / 1.6s fragment)."""
    from shortforge.models import Segment, Transcript, Candidate
    from shortforge.select.select import _MIN_CLIP_SECONDS
    segs = [Segment(i * 2.0, i * 2.0 + 2.0, f"standalone thought {i} lands cleanly.")
            for i in range(20)]
    tr = Transcript("en", 40.0, segs)
    cands = [Candidate(s.start, s.end, 0.9 if i % 3 == 0 else 0.2, "hook")
             for i, s in enumerate(segs)]
    cfg = _cfg()
    cfg.override("select.target_duration", 2)     # would be a fragment if honoured literally
    cfg.override("select.tolerance", 1)
    cfg.override("select.num_clips", 4)
    clips = build_clips(tr, cands, cfg, "h")
    assert clips
    for c in clips:
        assert (c.end - c.start) >= _MIN_CLIP_SECONDS - 1e-6


# --- "I asked for 2 minutes and got 15" -------------------------------------- #
# Clips are grown by whole ASR segments and a segment was never split, so ONE
# over-long segment became one over-long clip. An ASR that returns a single block
# for the whole file (an API answering with `text` and no `segments`) turned
# "3 clips of 2 minutes" into a single 15-minute clip — and only one of them.

def _blob(duration, words=3000):
    from shortforge.models import Segment, Transcript
    text = " ".join(f"w{i}" for i in range(words))
    return Transcript(language="en", duration=duration,
                      segments=[Segment(0.0, duration, text)])


def test_one_giant_segment_does_not_become_one_giant_clip():
    from shortforge.config import Config
    from shortforge.select.select import build_clips

    cfg = Config.load()
    cfg.override("select.target_duration", 120)
    cfg.override("select.num_clips", 3)
    clips = build_clips(_blob(900.0), [], cfg, "h")

    assert len(clips) == 3                               # the count is delivered
    tol = float(cfg.get("select.tolerance", 12))
    for c in clips:
        assert c.duration <= 120 + tol + 0.5, f"clip {c.clip_id} is {c.duration:.0f}s"
        assert c.duration >= 60                          # and not a scrap either


def test_clips_from_a_split_segment_do_not_overlap():
    from shortforge.config import Config
    from shortforge.select.select import build_clips

    cfg = Config.load()
    cfg.override("select.target_duration", 60)
    cfg.override("select.num_clips", 5)
    clips = build_clips(_blob(900.0), [], cfg, "h")
    for a, b in zip(clips, clips[1:]):
        assert b.start >= a.end - 0.001


def test_splitting_keeps_word_timings_in_the_right_piece():
    from shortforge.models import Segment, Word
    from shortforge.select.select import split_long_segments

    words = [Word(float(i), float(i) + 0.9, f"w{i}") for i in range(120)]
    seg = Segment(0.0, 120.0, " ".join(w.text for w in words), words)
    pieces = split_long_segments([seg], max_len=40.0, piece=30.0)

    assert len(pieces) == 4
    assert sum(len(p.words) for p in pieces) == 120       # no word lost or duplicated
    for p in pieces:
        for w in p.words:
            mid = (w.start + w.end) / 2.0
            assert p.start <= mid < p.end                 # each word in its own piece


def test_a_normal_transcript_is_left_completely_alone():
    """Splitting must not disturb sources that were already fine."""
    from shortforge.models import Segment
    from shortforge.select.select import split_long_segments

    segs = [Segment(float(i * 5), float(i * 5 + 4), f"line {i}") for i in range(20)]
    assert split_long_segments(segs, max_len=60.0, piece=45.0) == segs


def test_wordless_text_is_divided_not_repeated_on_every_piece():
    from shortforge.models import Segment
    from shortforge.select.select import split_long_segments

    seg = Segment(0.0, 100.0, " ".join(f"t{i}" for i in range(100)))
    pieces = split_long_segments([seg], max_len=30.0, piece=25.0)
    joined = " ".join(p.text for p in pieces).split()
    assert joined == seg.text.split()                    # exactly once, in order
