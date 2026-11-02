"""Hook-detection rubric (M3).

Shared between the heuristic scorer and the LLM scorer so both optimise for the
same notion of a strong short-form hook: opens a curiosity loop, makes a strong
or surprising claim, cites a number/stat, addresses the viewer, has an emotional
peak, and is self-contained.
"""

from __future__ import annotations

# Curiosity / open-loop phrases — the strongest single signal.
HOOK_PHRASES = [
    "you won't believe",
    "here's why",
    "here is why",
    "the secret",
    "the truth about",
    "the truth is",
    "what happens when",
    "the reason",
    "did you know",
    "the biggest mistake",
    "most people",
    "nobody tells you",
    "no one tells you",
    "the problem with",
    "this is how",
    "how to",
    "the one thing",
    "what if",
    "let me tell you",
    "the trick",
    "the key to",
    "watch this",
]

# Strong-claim / superlative words.
STRONG_WORDS = [
    "never", "always", "everyone", "nobody", "everything", "nothing",
    "best", "worst", "biggest", "smallest", "most", "least", "only",
    "secret", "truth", "proven", "guaranteed", "instantly", "forever",
    "impossible", "essential", "critical", "ultimate", "shocking",
]

# Emotional / emphasis words that mark a peak.
EMOTION_WORDS = [
    "amazing", "incredible", "insane", "crazy", "unbelievable", "terrifying",
    "beautiful", "powerful", "painful", "love", "hate", "fear", "afraid",
    "angry", "happy", "sad", "wow", "shocked", "surprised", "dangerous",
]

NUMBER_WORDS = [
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "hundred", "thousand", "million", "billion", "percent", "half",
    "double", "triple",
]

# Rubric text handed to the LLM scorer.
LLM_RUBRIC = """\
You are selecting the strongest standalone moments from a video transcript to
cut into short vertical clips (Reels/Shorts). Score each candidate moment on
hook strength for the FIRST 1-2 seconds and on being self-contained.

A strong moment (higher score) does one or more of:
- opens a curiosity loop or asks a compelling question
- makes a strong, surprising, or contrarian claim
- states a concrete number, statistic, or result
- has a clear emotional peak, punchline, or payoff
- directly addresses the viewer ("you")
- begins and ends on a clean sentence boundary (self-contained meaning)

A weak moment (lower score) is mid-thought, rambling, filler, setup with no
payoff, or depends on earlier context to make sense.
"""

# Normalisation scale for the heuristic raw score -> 0..1.
HEURISTIC_SCALE = 8.0
