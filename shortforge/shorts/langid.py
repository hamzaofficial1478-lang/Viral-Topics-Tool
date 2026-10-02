"""Which language a channel's Shorts are in — from their titles, offline.

Short titles are short, full of hashtags and emoji, and often mix languages,
so this works the way a person skimming them would: first by WRITING SYSTEM
(Arabic letters, Devanagari, Hangul … each narrows it to one or two languages),
and for the Latin alphabet by the little common words every sentence uses.

One case gets special handling because it matters for this operator: Hindi and
Urdu written in the Latin alphabet ("ye kaunsa paneer hai", "recipe kaise
banaye"). It is the same spoken language in either script and cannot be told
apart in Roman letters, so it is reported as ``hi-Latn`` — "Hindi/Urdu (Roman
script)" — and counts as a match for either.

No dependency, no model, no network: discovery checks dozens of channels and
this runs on every one.
"""

from __future__ import annotations

import re
import unicodedata

# (code, label) — the choices offered in the UI. "" = any language.
LANGUAGES: list[tuple[str, str]] = [
    ("", "Any language"),
    ("en", "English"), ("ur", "Urdu"), ("hi", "Hindi"), ("ar", "Arabic"),
    ("es", "Spanish"), ("pt", "Portuguese"), ("fr", "French"), ("de", "German"),
    ("it", "Italian"), ("tr", "Turkish"), ("id", "Indonesian / Malay"), ("ru", "Russian"),
    ("fa", "Persian"), ("bn", "Bengali"), ("pa", "Punjabi"), ("ta", "Tamil"), ("te", "Telugu"),
    ("ml", "Malayalam"), ("kn", "Kannada"), ("gu", "Gujarati"), ("mr", "Marathi"),
    ("ja", "Japanese"), ("ko", "Korean"), ("zh", "Chinese"), ("th", "Thai"),
    ("vi", "Vietnamese"), ("tl", "Filipino"), ("nl", "Dutch"), ("pl", "Polish"),
    ("uk", "Ukrainian"), ("el", "Greek"), ("he", "Hebrew"),
]
_LABEL = dict(LANGUAGES)
_LABEL["hi-Latn"] = "Hindi/Urdu (Roman script)"


def label(code: str) -> str:
    """Name of a DETECTED language ("" = couldn't tell)."""
    if not code:
        return "unknown"
    return _LABEL.get(code, code)


# Same-language-in-another-script and close-enough matches. A channel detected
# as the key counts as a match when the operator asked for any of the values.
_MATCHES = {
    "hi-Latn": {"hi", "ur"},
    "mr": {"mr", "hi"},          # both Devanagari; titles often can't separate them
    "ms": {"id"},
}


def matches(detected: str, wanted: str) -> bool:
    if not wanted:
        return True
    if not detected:
        return False
    if detected == wanted:
        return True
    return wanted in _MATCHES.get(detected, set())


# --- writing systems ---------------------------------------------------------- #

_SCRIPTS = [
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    ("devanagari", ((0x0900, 0x097F),)),
    ("bengali", ((0x0980, 0x09FF),)),
    ("gurmukhi", ((0x0A00, 0x0A7F),)),
    ("gujarati", ((0x0A80, 0x0AFF),)),
    ("tamil", ((0x0B80, 0x0BFF),)),
    ("telugu", ((0x0C00, 0x0C7F),)),
    ("kannada", ((0x0C80, 0x0CFF),)),
    ("malayalam", ((0x0D00, 0x0D7F),)),
    ("thai", ((0x0E00, 0x0E7F),)),
    ("hangul", ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))),
    ("kana", ((0x3040, 0x30FF),)),
    ("han", ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))),
    ("cyrillic", ((0x0400, 0x04FF),)),
    ("greek", ((0x0370, 0x03FF),)),
    ("hebrew", ((0x0590, 0x05FF),)),
]
_SCRIPT_LANG = {"devanagari": "hi", "bengali": "bn", "gurmukhi": "pa", "gujarati": "gu",
                "tamil": "ta", "telugu": "te", "kannada": "kn", "malayalam": "ml",
                "thai": "th", "hangul": "ko", "greek": "el", "hebrew": "he"}

_URDU_ONLY = set("ٹڈڑںےۓھ")
_ARABIC_ONLY = set("ةىأإؤ")
_UKRAINIAN_ONLY = set("іїєґІЇЄҐ")


def _script_of(ch: str) -> str | None:
    o = ord(ch)
    for name, ranges in _SCRIPTS:
        for lo, hi in ranges:
            if lo <= o <= hi:
                return name
    if ch.isalpha() and unicodedata.name(ch, "").startswith("LATIN"):
        return "latin"
    return None


# --- Latin-alphabet languages: their commonest little words ------------------ #

_STOP = {
    "en": "the and to of a in is it you this that for with on my how what why your i be are "
          "was best make can from at so do not all just will when one new day get",
    "hi-Latn": "hai hain ka ki ke ko se mein me aur ye yeh kya nahi nhi bhi ho tha thi karo "
               "kar wala wali kaise kaisa banaye bana apna apni sab bahut bhai ab jab tak ek "
               "hum tum aap mera meri tera kuch koi wale kare kiya gaya diya liye sath",
    "es": "el la de que y en los las un una por con para es como del se no lo más mi tu al "
          "pero muy este esta te",
    "pt": "o a de que e do da em um uma para com não os as no na por mais se como meu "
          "minha você isso muito é",
    "fr": "le la les de des et un une du en est que pour pas dans ce il je qui sur avec au "
          "mon ma trop c'est",
    "de": "der die das und ist ich nicht ein eine zu mit den von auf es sie du wie so für "
          "mein auch was",
    "it": "il la di che e un una per non con del della è sono mi ti come questo ma più",
    "id": "yang dan ini itu di ke dari untuk dengan tidak ada aku kamu saya apa bisa juga "
          "sudah banget enak cara",
    "tr": "ve bir bu da de için ile çok ne mi gibi ben sen var yok daha nasıl",
    "nl": "de het een en van ik je niet is dat op te met voor zijn maar",
    "pl": "i w na nie się z to jest że do jak co ale po tak",
    "vi": "và của là có không một những cho này được với người bạn tôi",
    "tl": "ang ng mga sa na at ko mo ito hindi ako ikaw",
}
_STOP_SETS = {k: set(v.split()) for k, v in _STOP.items()}
# Letters that point straight at one language.
_DIACRITIC = {"ñ": "es", "¿": "es", "¡": "es", "ã": "pt", "õ": "pt", "ç": "pt", "ß": "de",
              "ğ": "tr", "ş": "tr", "ı": "tr", "ư": "vi", "ơ": "vi", "đ": "vi", "ł": "pl",
              "ś": "pl", "ź": "pl", "ż": "pl"}

_NOISE = re.compile(r"https?://\S+|[#@]\S+")
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


def detect(texts: list[str]) -> tuple[str, float]:
    """``(language_code, confidence 0..1)`` for a channel's titles, or
    ``("", 0.0)`` when there isn't enough to go on. Hashtags and handles are
    ignored — #food or #shorts says nothing about the language spoken."""
    text = " ".join(_NOISE.sub(" ", t or "") for t in texts if t)
    if not text.strip():
        return "", 0.0

    counts: dict[str, int] = {}
    for ch in text:
        sc = _script_of(ch)
        if sc:
            counts[sc] = counts.get(sc, 0) + 1
    letters = sum(counts.values())
    if not letters:
        return "", 0.0

    top, n = max(counts.items(), key=lambda kv: kv[1])
    share = round(n / letters, 2)
    if top != "latin" and share >= 0.25:
        if top == "arabic":
            if any(c in _URDU_ONLY for c in text):
                return "ur", share
            if any(c in "پچژگ" for c in text) and not any(c in _ARABIC_ONLY for c in text):
                return "fa", share
            return "ar", share
        if top in ("kana",):
            return "ja", share
        if top == "han":
            return ("ja" if counts.get("kana") else "zh"), share
        if top == "cyrillic":
            return ("uk" if any(c in _UKRAINIAN_ONLY for c in text) else "ru"), share
        return _SCRIPT_LANG.get(top, ""), share

    # Latin alphabet: vote with common words, nudged by tell-tale letters.
    words = [w.lower() for w in _WORD.findall(text)]
    if not words:
        return "", 0.0
    score = {lang: 0.0 for lang in _STOP_SETS}
    for w in words:
        for lang, stops in _STOP_SETS.items():
            if w in stops:
                score[lang] += 1
    low = text.lower()
    for ch, lang in _DIACRITIC.items():
        if ch in low:
            score[lang] += 2
    best = max(score, key=score.get)
    hits = score[best]
    total = sum(score.values())
    if hits < 2:
        return "", 0.0
    # Hinglish: English words AND Roman Hindi/Urdu together is how most
    # Pakistani/Indian Shorts are titled. Roman Hindi/Urdu is the spoken
    # language there, so it wins whenever it is clearly present.
    if best == "en" and score["hi-Latn"] >= max(2, 0.35 * score["en"]):
        best, hits = "hi-Latn", score["hi-Latn"]
    return best, (round(hits / total, 2) if total else 0.0)
