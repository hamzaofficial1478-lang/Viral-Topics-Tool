"""Phase 3 pure logic: translation dispatch, QC, TTS timing math."""

from shortforge.config import Config
from shortforge.localize.translate import translate_segments, LANG_NAMES
from shortforge.localize.tts import _atempo_chain
from shortforge.models import Segment
from shortforge import qc


def test_translate_identity_when_same_lang():
    segs = [Segment(0, 1, "hello"), Segment(1, 2, "world")]
    res = translate_segments(segs, "en", "en", Config.load())
    assert [s.text for s in res.segments] == ["hello", "world"]
    assert res.segments[0] is not segs[0]  # returns copies
    assert res.backend == "identity" and res.cacheable is False


def test_lang_names_cover_targets():
    for code in ("en", "de", "it", "es", "ja", "ar"):
        assert code in LANG_NAMES


def test_atempo_chain_multiplies_to_speed():
    for speed in (1.0, 1.5, 2.5, 3.0, 0.4):
        chain = _atempo_chain(speed)
        factors = [float(p.split("=")[1]) for p in chain.split(",")]
        product = 1.0
        for f in factors:
            assert 0.5 <= f <= 2.0
            product *= f
        assert abs(product - max(0.33, min(3.0, speed))) < 1e-3


def test_qc_flags_profanity():
    flags = qc.brand_safety_flags("this is fucking great")
    assert flags and "profanity" in flags[0]


def test_qc_clean_text_no_flags():
    assert qc.brand_safety_flags("a wholesome motivational message") == []


def test_review_clip_status():
    clean = qc.review_clip("great advice for founders", "en", "none")
    assert clean["status"] == "pending_review" and clean["flags"] == []
    flagged = qc.review_clip("this shit is wild", "en", "duck")
    assert flagged["status"] == "flagged"
    assert flagged["dub_method"] == "duck"
