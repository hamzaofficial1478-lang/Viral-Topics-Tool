"""Countries, as YouTube names them on a channel's About page.

The About page is fetched in English, so a channel's country arrives as a name
("Pakistan", "United Arab Emirates", "Türkiye"). Matching is by normalised name
with the common alternatives, so "UAE", "Turkey" and "Türkiye" all line up.
"""

from __future__ import annotations

import unicodedata

# (code, display name, other names YouTube or people use)
COUNTRIES: list[tuple[str, str, tuple[str, ...]]] = [
    ("PK", "Pakistan", ()), ("IN", "India", ()), ("AE", "United Arab Emirates", ("uae", "emirates")),
    ("SA", "Saudi Arabia", ("ksa",)), ("QA", "Qatar", ()), ("KW", "Kuwait", ()), ("OM", "Oman", ()),
    ("BH", "Bahrain", ()), ("EG", "Egypt", ()), ("JO", "Jordan", ()), ("LB", "Lebanon", ()),
    ("IQ", "Iraq", ()), ("MA", "Morocco", ()), ("DZ", "Algeria", ()), ("TN", "Tunisia", ()),
    ("TR", "Türkiye", ("turkey", "turkiye")), ("IR", "Iran", ()), ("AF", "Afghanistan", ()),
    ("BD", "Bangladesh", ()), ("LK", "Sri Lanka", ()), ("NP", "Nepal", ()),
    ("ID", "Indonesia", ()), ("MY", "Malaysia", ()), ("SG", "Singapore", ()),
    ("PH", "Philippines", ()), ("TH", "Thailand", ()), ("VN", "Vietnam", ("viet nam",)),
    ("CN", "China", ()), ("HK", "Hong Kong", ()), ("TW", "Taiwan", ()), ("JP", "Japan", ()),
    ("KR", "South Korea", ("korea", "republic of korea")), ("AU", "Australia", ()),
    ("NZ", "New Zealand", ()), ("US", "United States", ("usa", "us", "united states of america")),
    ("CA", "Canada", ()), ("MX", "Mexico", ()), ("BR", "Brazil", ()), ("AR", "Argentina", ()),
    ("CO", "Colombia", ()), ("CL", "Chile", ()), ("PE", "Peru", ()),
    ("GB", "United Kingdom", ("uk", "great britain", "britain", "england")),
    ("IE", "Ireland", ()), ("FR", "France", ()), ("DE", "Germany", ()), ("IT", "Italy", ()),
    ("ES", "Spain", ()), ("PT", "Portugal", ()), ("NL", "Netherlands", ("holland",)),
    ("BE", "Belgium", ()), ("CH", "Switzerland", ()), ("AT", "Austria", ()), ("SE", "Sweden", ()),
    ("NO", "Norway", ()), ("DK", "Denmark", ()), ("FI", "Finland", ()), ("PL", "Poland", ()),
    ("RU", "Russia", ()), ("UA", "Ukraine", ()), ("GR", "Greece", ()), ("NG", "Nigeria", ()),
    ("KE", "Kenya", ()), ("ZA", "South Africa", ()), ("GH", "Ghana", ()),
]
_NAME = {c: n for c, n, _ in COUNTRIES}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace(".", " ").replace("-", " ").split())


_BY_NAME = {}
for _c, _n, _alts in COUNTRIES:
    for _x in (_n, *_alts):
        _BY_NAME[_norm(_x)] = _c


def name(code: str) -> str:
    return _NAME.get((code or "").upper(), code or "")


def code_for(text: str) -> str:
    """'Pakistan' / 'UAE' / 'Türkiye' / 'pk' → 'PK'; '' when unknown."""
    t = _norm(text)
    if not t:
        return ""
    if t.upper() in _NAME:
        return t.upper()
    return _BY_NAME.get(t, "")


def choices() -> list[tuple[str, str]]:
    """(code, label) for the UI, "" = any country."""
    return [("", "Any country")] + sorted(((c, n) for c, n, _ in COUNTRIES), key=lambda x: x[1])
