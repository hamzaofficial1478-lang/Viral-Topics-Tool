"""M3 heuristic hook scorer."""

from shortforge.detect.heuristic import score_segment_text, detect


def test_strong_hook_beats_filler():
    strong, _, _ = score_segment_text(
        "Did you know that ninety percent of startups fail within ten years?"
    )
    filler, _, _ = score_segment_text("Um, so, yeah.")
    assert strong > filler
    assert strong > 0.5


def test_scores_are_normalised():
    for text in ["", "hello", "The secret to success is never giving up on your goals!"]:
        s, _, _ = score_segment_text(text)
        assert 0.0 <= s <= 1.0


def test_question_and_number_signals():
    _, _, signals = score_segment_text("What are the 3 things you must do?")
    assert signals.get("question") is True
    assert signals.get("number") is True
    assert signals.get("second_person") is True


def test_detect_ranks_descending(sample_transcript):
    cands = detect(sample_transcript, min_score=0.0)
    assert len(cands) == len(sample_transcript.segments)
    scores = [c.score for c in cands]
    assert scores == sorted(scores, reverse=True)
    # The "Did you know ... ninety percent ... fail?" line should top the list.
    assert "Did you know" in _text_for(sample_transcript, cands[0].start)


def _text_for(transcript, start):
    for s in transcript.segments:
        if abs(s.start - start) < 1e-6:
            return s.text
    return ""
