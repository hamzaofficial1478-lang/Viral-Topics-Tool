"""M6 — Translation (Phase 3, hardened per A2).

Pluggable translation: Claude when a key is present (best), Argos Translate
offline (CPU) otherwise. Segment timing is preserved; only text changes.

A2 rules:
- A failed or passthrough "translation" is NEVER treated as valid and NEVER
  cached. If the target language differs from the source and no backend can
  translate, we raise ``TranslationUnavailable`` (the caller aborts) unless the
  operator passed ``--allow-untranslated``.
- Every result carries provenance (backend + version) so the cache key can tell
  a real translation apart from a passthrough, and a sanity check flags a
  suspicious result (mostly source-identical) so it is warned about and not cached.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import Config
from ..models import Segment
from ..utils import ShortForgeError, log

LANG_NAMES = {
    "en": "English", "de": "German", "it": "Italian", "es": "Spanish",
    "ja": "Japanese", "ar": "Arabic", "fr": "French", "pt": "Portuguese",
    "hi": "Hindi", "ur": "Urdu", "tr": "Turkish", "ru": "Russian",
}


class TranslationUnavailable(ShortForgeError):
    """Raised when the requested translation cannot be produced (A2 fail-loud)."""


@dataclass
class TranslationResult:
    segments: list[Segment]
    backend: str            # "llm" | "argos" | "identity" | "none"
    version: str = "-"
    passthrough: bool = False   # True = source text kept (not a real translation)
    identical_ratio: float = 0.0

    @property
    def cacheable(self) -> bool:
        # Only a real, non-passthrough translation may be cached.
        return not self.passthrough and self.backend in ("llm", "argos")


def _llm_available() -> bool:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _argos_importable() -> bool:
    try:
        import argostranslate  # noqa: F401
        return True
    except ImportError:
        return False


def _argos_version() -> str:
    try:
        from importlib.metadata import version
        return version("argostranslate")
    except Exception:  # noqa: BLE001
        return "unknown"


def resolve_backend(cfg: Config, src: str, tgt: str) -> tuple[str, str]:
    """Which backend WILL run for src->tgt, without translating (for cache keys).

    Returns (name, version). ("none","-") means nothing can translate.
    """
    src = (src or "en").split("-")[0]
    tgt = (tgt or "en").split("-")[0]
    if src == tgt:
        return ("identity", "-")
    backend = cfg.get("localize.translate_backend", "auto")
    if backend in ("auto", "llm") and _llm_available():
        return ("llm", str(cfg.get("detect.llm_model", "claude")))
    if backend in ("auto", "argos") and _argos_importable():
        return ("argos", f"argos-{_argos_version()}")
    return ("none", "-")


def _identical_ratio(src_texts: list[str], out_texts: list[str]) -> float:
    pairs = [(a, b) for a, b in zip(src_texts, out_texts) if a.strip()]
    if not pairs:
        return 0.0
    same = sum(1 for a, b in pairs if a.strip() == b.strip())
    return same / len(pairs)


def translate_segments(
    segments: list[Segment], src: str, tgt: str, cfg: Config,
    allow_untranslated: bool = False,
) -> TranslationResult:
    """Translate each segment's text src->tgt (timing kept). See A2 rules."""
    src = (src or "en").split("-")[0]
    tgt = (tgt or "en").split("-")[0]
    if src == tgt or not segments:
        segs = [Segment(s.start, s.end, s.text, list(s.words)) for s in segments]
        return TranslationResult(segs, backend="identity", passthrough=True)

    backend = cfg.get("localize.translate_backend", "auto")
    texts = [s.text for s in segments]
    out: list[str] | None = None
    used = "none"
    version = "-"

    if backend in ("auto", "llm") and _llm_available():
        try:
            out = _llm_translate(texts, src, tgt, cfg)
            used, version = "llm", str(cfg.get("detect.llm_model", "claude"))
            log.info("translated %d segments with Claude (%s->%s)", len(texts), src, tgt)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM translation failed (%s); trying offline Argos", e)

    if out is None and backend in ("auto", "argos"):
        try:
            out = _argos_translate(texts, src, tgt)
            used, version = "argos", f"argos-{_argos_version()}"
            log.info("translated %d segments with Argos (%s->%s)", len(texts), src, tgt)
        except Exception as e:  # noqa: BLE001
            log.warning("Argos translation unavailable (%s)", e)

    if out is None:
        msg = (
            f"No translation backend available for {src}->{tgt}. The output "
            f"language ({tgt}) differs from the source ({src}); emitting "
            f"source-language text would be wrong. Install one of:\n"
            f"  - Argos (offline):  pip install argostranslate\n"
            f"  - Claude:           set ANTHROPIC_API_KEY and pip install anthropic\n"
            f"Or pass --allow-untranslated to proceed with the source text."
        )
        if not allow_untranslated:
            raise TranslationUnavailable(msg)
        log.warning("ALLOW-UNTRANSLATED: %s", msg)
        segs = [Segment(s.start, s.end, s.text, []) for s in segments]
        return TranslationResult(segs, backend="none", passthrough=True,
                                 identical_ratio=1.0)

    # Sanity check: a real translation should mostly differ from the source.
    ratio = _identical_ratio(texts, out)
    passthrough = False
    if ratio > 0.30:
        idents = [i for i, (a, b) in enumerate(zip(texts, out)) if a.strip() == b.strip()]
        log.warning(
            "translation looks like a passthrough failure: %.0f%% of segments are "
            "identical to the source (segments %s). NOT caching this result.",
            ratio * 100, idents[:10],
        )
        passthrough = True

    segs = [Segment(s.start, s.end, out[i] if i < len(out) else s.text, [])
            for i, s in enumerate(segments)]
    return TranslationResult(segs, backend=used, version=version,
                             passthrough=passthrough, identical_ratio=ratio)


def _llm_translate(texts: list[str], src: str, tgt: str, cfg: Config) -> list[str]:
    import anthropic

    client = anthropic.Anthropic()
    model = cfg.get("detect.llm_model", "claude-opus-4-8")
    tgt_name = LANG_NAMES.get(tgt, tgt)
    schema = {
        "type": "object",
        "properties": {"translations": {"type": "array", "items": {"type": "string"}}},
        "required": ["translations"],
        "additionalProperties": False,
    }
    numbered = "\n".join(f"{i}\t{t}" for i, t in enumerate(texts))
    prompt = (
        f"Translate each numbered line to {tgt_name}. Keep it natural and concise "
        f"(short-form video captions/dub), one output per input line, same order. "
        f"Return only the translations.\n\n{numbered}"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    import json

    data = json.loads(next((b.text for b in resp.content if b.type == "text"), "{}"))
    tr = data.get("translations", [])
    if len(tr) != len(texts):
        raise ValueError(f"translation count mismatch: {len(tr)} != {len(texts)}")
    return [str(x) for x in tr]


def _argos_translate(texts: list[str], src: str, tgt: str) -> list[str]:
    import argostranslate.package as pkg
    import argostranslate.translate as tr

    installed = {l.code for l in tr.get_installed_languages()}
    if src not in installed or tgt not in installed:
        log.info("Argos: %s->%s language pack not installed; downloading it now", src, tgt)
        pkg.update_package_index()
        avail = pkg.get_available_packages()
        p = next((x for x in avail if x.from_code == src and x.to_code == tgt), None)
        if p is None:
            raise RuntimeError(
                f"no Argos language pack for {src}->{tgt}; a pivot via English "
                f"may be required, or install the pair manually."
            )
        pkg.install_from_path(p.download())
        log.info("Argos: installed %s->%s language pack", src, tgt)

    langs = tr.get_installed_languages()
    from_lang = next(l for l in langs if l.code == src)
    to_lang = next(l for l in langs if l.code == tgt)
    translator = from_lang.get_translation(to_lang)
    return [translator.translate(t) for t in texts]
