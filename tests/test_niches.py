"""Niche suggestion ranking + filters."""

from shortforge.research import NICHES, categories, suggest


def test_suggest_returns_scored_and_ranked():
    out = suggest(top=8)
    assert 1 <= len(out) <= 8
    # opportunity descending
    scores = [n["opportunity"] for n in out]
    assert scores == sorted(scores, reverse=True)
    # each entry is complete
    for n in out:
        for k in ("name", "category", "growth", "competition",
                  "monetization", "subniches", "why", "opportunity"):
            assert k in n
        assert n["subniches"]


def test_top_of_opportunity_is_low_competition_high_value():
    top = suggest(top=1)[0]
    # The single best "room to grow" pick should not be a high-competition space.
    assert top["competition"] in ("low", "medium")
    assert top["monetization"] in ("high", "very high")


def test_category_filter():
    out = suggest(category="Finance", top=20)
    assert out and all("Finance" in n["category"] for n in out)


def test_low_competition_filter():
    out = suggest(low_competition=True, top=20)
    assert out and all(n["competition"] == "low" for n in out)


def test_sort_by_monetization_puts_very_high_first():
    out = suggest(sort_by="monetization", top=5)
    assert out[0]["monetization"] == "very high"


def test_top_limit_respected():
    assert len(suggest(top=3)) == 3


def test_categories_nonempty():
    cats = categories()
    assert "Finance" in cats and "Tech/AI" in cats


def test_opportunity_prefers_growth_and_low_competition():
    from shortforge.research.niches import Niche
    crowded = Niche("A", "X", "steady", "high", "medium", "", ["a"], "")
    open_lane = Niche("B", "X", "hot", "low", "very high", "", ["b"], "")
    assert open_lane.opportunity() > crowded.opportunity()
