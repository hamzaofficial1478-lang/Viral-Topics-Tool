"""Shared test fixtures."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shortforge.models import Segment, Transcript, Word  # noqa: E402


def _seg(start: float, end: float, text: str) -> Segment:
    tokens = text.split()
    per = (end - start) / max(1, len(tokens))
    words = [
        Word(start=start + i * per, end=start + (i + 1) * per, text=t)
        for i, t in enumerate(tokens)
    ]
    return Segment(start=start, end=end, text=text, words=words)


@pytest.fixture
def sample_transcript() -> Transcript:
    texts = [
        "Today I want to talk about something important.",
        "Here is why most people never reach their goals.",
        "The biggest mistake you can make is giving up too early.",
        "Did you know that ninety percent of startups fail?",
        "Um, so, yeah.",
        "The secret to success is showing up every single day.",
        "This is absolutely incredible and changes everything.",
        "Anyway that is all for now thanks for watching.",
    ]
    segs = []
    t = 0.0
    for tx in texts:
        d = max(3.0, len(tx.split()) * 0.5)
        segs.append(_seg(t, t + d, tx))
        t += d
    return Transcript(language="en", duration=t, segments=segs)
