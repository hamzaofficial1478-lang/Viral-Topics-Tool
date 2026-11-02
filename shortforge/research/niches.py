"""Niche suggestions — where short-form attention (and money) is growing.

A curated, ranked guide to short-form niches, scored on three levers a creator
actually cares about:

- **growth**    — how fast audience demand is expanding (structural, not a fad).
- **competition** — how crowded it is (lower = easier to break in / grow).
- **monetization** — RPM + sponsorship/product potential (how well it pays).

An "opportunity" score combines them so *growing + still-uncrowded + pays well*
floats to the top — i.e. the niches with the most room to grow. This is a
curated evergreen guide (offline, no key), not a live trend scraper; live
day-to-day trends need an external data source. The optional Claude pass
personalises the list to your interests/existing channel.

Each entry also lists **sub-niches**: the specific, lower-competition angles
inside a broad space, which is usually where a new creator actually wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..utils import log

_GROWTH = {"hot": 3.0, "rising": 2.2, "steady": 1.2}
_MONEY = {"very high": 4.0, "high": 3.0, "medium": 2.0, "low": 1.0}
_COMP_BONUS = {"low": 2.0, "medium": 1.0, "high": 0.0}


@dataclass
class Niche:
    name: str
    category: str
    growth: str          # hot | rising | steady
    competition: str     # low | medium | high
    monetization: str    # very high | high | medium | low
    audience: str
    subniches: list[str]
    why: str

    def opportunity(self) -> float:
        """Higher = more room to grow: growth + monetization + uncrowdedness."""
        return round(
            _GROWTH.get(self.growth, 1.0) * 1.2
            + _MONEY.get(self.monetization, 1.0) * 1.0
            + _COMP_BONUS.get(self.competition, 0.0) * 1.3,
            2,
        )

    def to_dict(self) -> dict[str, Any]:
        d = {
            "name": self.name,
            "category": self.category,
            "growth": self.growth,
            "competition": self.competition,
            "monetization": self.monetization,
            "audience": self.audience,
            "subniches": self.subniches,
            "why": self.why,
            "opportunity": self.opportunity(),
        }
        return d


# Curated set. Signals are structural (evergreen demand + how it pays), chosen to
# be defensible rather than chasing a passing trend.
NICHES: list[Niche] = [
    Niche("AI tools & workflows", "Tech/AI", "hot", "medium", "very high",
          "professionals, founders, marketers wanting to work faster",
          ["AI for a specific job (lawyers, realtors, teachers)",
           "no-code automations", "prompt walkthroughs", "AI news in 60s"],
          "Huge, still-rising demand and high B2B/software RPM + sponsors."),
    Niche("Personal finance & investing", "Finance", "hot", "high", "very high",
          "20-40s wanting to earn, save, and invest",
          ["finance for a country/currency", "index investing basics",
           "side-income breakdowns", "money mistakes"],
          "Top-tier RPM and sponsors; win by going specific vs the crowd."),
    Niche("Money for a specific audience", "Finance", "rising", "low", "very high",
          "underserved groups (nurses, immigrants, teens, freelancers)",
          ["taxes for freelancers", "investing for teens",
           "benefits for a profession", "budgeting for parents"],
          "Same great finance economics with far less competition."),
    Niche("Health, longevity & habits", "Health", "hot", "high", "high",
          "broad audience chasing energy, sleep, and long-term health",
          ["sleep optimization", "longevity basics", "habit systems",
           "desk-worker mobility"],
          "Massive interest and strong supplement/app sponsorships."),
    Niche("Fitness for a specific body/goal", "Health", "rising", "medium", "high",
          "people who feel ignored by generic fitness content",
          ["over-40 strength", "postpartum fitness", "home/no-equipment",
           "mobility for runners"],
          "Generic fitness is saturated; specific bodies/goals are wide open."),
    Niche("Mental health & self-improvement", "Lifestyle", "rising", "high", "medium",
          "teens–30s working on focus, anxiety, discipline",
          ["dopamine/focus", "journaling systems", "stoicism in practice",
           "beating procrastination"],
          "Enormous demand; monetize via courses/community more than RPM."),
    Niche("Career & job skills", "Business", "rising", "medium", "very high",
          "job seekers and early-career professionals",
          ["resume/LinkedIn fixes", "interview answers", "in-demand skills",
           "salary negotiation"],
          "Recruiters/course sponsors pay well; evergreen search demand."),
    Niche("Solopreneur & side hustles", "Business", "hot", "high", "very high",
          "people building income outside a 9-5",
          ["a real side-hustle teardown", "digital products",
           "freelancing niches", "first-$1k stories"],
          "High commercial intent = strong RPM, courses, and affiliates."),
    Niche("Coding & developer content", "Tech/AI", "steady", "medium", "high",
          "students and working developers",
          ["one language/framework", "build-in-public", "dev tools in 60s",
           "system-design basics"],
          "Loyal audience and excellent dev-tool sponsorships."),
    Niche("Tech reviews & how-tos", "Tech/AI", "steady", "high", "high",
          "buyers researching gadgets, apps, and software",
          ["budget gear", "app alternatives", "hidden features",
           "setup/optimization guides"],
          "High buyer intent → affiliates; differentiate by being specific."),
    Niche("Language learning", "Education", "rising", "medium", "high",
          "millions learning a second language",
          ["one language via short skits", "phrases travelers need",
           "common mistakes", "slang explained"],
          "Global evergreen demand and app sponsorships."),
    Niche("Explainer / 'how things work'", "Education", "rising", "medium", "medium",
          "curious general audience, strong shareability",
          ["science in 60s", "history moments", "how X is made",
           "economics explained"],
          "Very shareable = fast growth; monetize at scale + brand deals."),
    Niche("Study & productivity systems", "Education", "rising", "medium", "medium",
          "students and knowledge workers",
          ["study techniques", "note-taking apps", "exam prep",
           "focus routines"],
          "Loyal repeat audience; app/tool sponsorships."),
    Niche("Parenting & family", "Lifestyle", "rising", "medium", "high",
          "new and busy parents",
          ["newborn tips", "toddler activities", "parenting scripts",
           "family budgeting"],
          "High-trust audience brands love; underserved in short-form."),
    Niche("Food: fast & specific", "Food", "steady", "high", "medium",
          "everyone, but win on a clear angle",
          ["high-protein recipes", "one-pan/5-ingredient", "a cuisine",
           "budget meal prep"],
          "Saturated broadly; a sharp angle (protein, budget) still grows fast."),
    Niche("Home, DIY & organization", "Lifestyle", "rising", "medium", "high",
          "homeowners and renters improving their space",
          ["small-space hacks", "cleaning routines", "budget renovations",
           "renter-friendly DIY"],
          "Strong home/retail sponsorships and shareable transformations."),
    Niche("Pets & animal care", "Lifestyle", "steady", "medium", "medium",
          "pet owners (dogs/cats first)",
          ["training tips", "breed guides", "pet health myths",
           "budget pet care"],
          "Devoted audience and growing pet-brand sponsor budgets."),
    Niche("Real estate & housing", "Finance", "steady", "medium", "very high",
          "buyers, renters, and investors",
          ["first-time buyer tips", "a specific city market",
           "house-hacking", "renting smarter"],
          "Very high RPM and lead-gen value; go local to cut competition."),
    Niche("Sustainability & practical eco", "Lifestyle", "rising", "low", "medium",
          "value-driven millennials/Gen-Z",
          ["low-waste swaps", "saving energy/money", "repair-don't-replace",
           "eco on a budget"],
          "Rising demand, still uncrowded when framed as saving money."),
    Niche("Creator & marketing skills", "Business", "hot", "high", "very high",
          "creators and small businesses",
          ["short-form growth", "one platform's algorithm", "editing tips",
           "personal branding"],
          "Meta-niche with high software RPM; stand out with concrete results."),
]


def _match_category(n: Niche, category: str | None) -> bool:
    if not category:
        return True
    c = category.strip().lower()
    return c in n.category.lower() or c in n.name.lower()


def suggest(
    cfg: Config | None = None,
    *,
    category: str | None = None,
    top: int = 8,
    sort_by: str = "opportunity",
    low_competition: bool = False,
) -> list[dict[str, Any]]:
    """Return ranked niche suggestions as dicts (already scored)."""
    items = [n for n in NICHES if _match_category(n, category)]
    if low_competition:
        items = [n for n in items if n.competition == "low"] or items

    if sort_by == "growth":
        key = lambda n: (_GROWTH.get(n.growth, 0), n.opportunity())
    elif sort_by == "monetization":
        key = lambda n: (_MONEY.get(n.monetization, 0), n.opportunity())
    else:  # opportunity (default)
        key = lambda n: n.opportunity()

    ranked = sorted(items, key=key, reverse=True)
    return [n.to_dict() for n in ranked[: max(1, top)]]


def categories() -> list[str]:
    return sorted({n.category for n in NICHES})


def personalize(interests: str, cfg: Config, base: list[dict[str, Any]]) -> str | None:
    """Optional Claude pass: tailor the ranked list to the operator's interests.

    Returns a short markdown note, or None if Claude isn't available.
    """
    import os

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
        model = cfg.get("detect.llm_model", "claude-opus-4-8")
        names = ", ".join(b["name"] for b in base)
        prompt = (
            "You advise short-form creators on niche selection. Given the "
            f"creator's interests/channel: \"{interests}\".\n"
            f"Here is a shortlist of growing niches to consider: {names}.\n\n"
            "Recommend the 3 best fits for THIS creator and, for each, one "
            "specific low-competition sub-niche angle and a first-video idea. "
            "Be concrete and concise (markdown bullets)."
        )
        resp = client.messages.create(
            model=model, max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        return next((b.text for b in resp.content if b.type == "text"), None)
    except Exception as e:  # noqa: BLE001
        log.warning("niche personalization unavailable (%s)", e)
        return None


__all__ = ["Niche", "NICHES", "suggest", "categories", "personalize"]
