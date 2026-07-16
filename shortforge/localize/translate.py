"""M6 — Translation (Phase 3).

Pluggable ``translate_segments``: Claude when a key is present (best quality),
Argos Translate offline (CPU) otherwise, and an identity passthrough as a last
resort so the pipeline never hard-fails. Segment timing is preserved; only the
text changes.
"""

from __future__ import annotations

import os

from ..config import Config
from ..models import Segment
from ..utils import log

LANG_NAMES = {
    "en": "English", "de": "German", "it": "Italian", "es": "Spanish",
    "ja": "Japanese", "ar": "Arabic", "fr": "French", "pt": "Portuguese",
    "hi": "Hindi", "ur": "Urdu", "tr": "Turkish", "ru": "Russian",
}


def _llm_available() -> bool:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def translate_segments(
    segments: list[Segment], src: str, tgt: str, cfg: Config
) -> list[Segment]:
    """Translate each segment's text from ``src`` to ``tgt`` (timing kept)."""
    src = (src or "en").split("-")[0]
    tgt = (tgt or "en").split("-")[0]
    if src == tgt or not segments:
        return [Segment(s.start, s.end, s.text, list(s.words)) for s in segments]

    backend = cfg.get("localize.translate_backend", "auto")
    texts = [s.text for s in segments]
    out: list[str] | None = None

    if backend in ("auto", "llm") and (backend == "llm" or _llm_available()):
        try:
            out = _llm_translate(texts, src, tgt, cfg)
            log.info("translated %d segments with Claude (%s->%s)", len(texts), src, tgt)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM translation failed (%s); trying offline", e)

    if out is None and backend in ("auto", "argos"):
        try:
            out = _argos_translate(texts, src, tgt)
            log.info("translated %d segments with Argos (%s->%s)", len(texts), src, tgt)
        except Exception as e:  # noqa: BLE001
            log.warning("Argos translation unavailable (%s)", e)

    if out is None:
        log.warning(
            "no translation backend available — captions/dub will keep the source "
            "text. Set ANTHROPIC_API_KEY or install argostranslate for %s->%s.",
            src, tgt,
        )
        out = texts

    return [
        Segment(s.start, s.end, out[i] if i < len(out) else s.text, [])
        for i, s in enumerate(segments)
    ]


def _llm_translate(texts: list[str], src: str, tgt: str, cfg: Config) -> list[str]:
    import anthropic

    client = anthropic.Anthropic()
    model = cfg.get("detect.llm_model", "claude-opus-4-8")
    tgt_name = LANG_NAMES.get(tgt, tgt)
    schema = {
        "type": "object",
        "properties": {
            "translations": {"type": "array", "items": {"type": "string"}}
        },
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

    installed = {(l.code) for l in tr.get_installed_languages()}
    if src not in installed or tgt not in installed:
        pkg.update_package_index()
        avail = pkg.get_available_packages()
        p = next((x for x in avail if x.from_code == src and x.to_code == tgt), None)
        if p is None:
            raise RuntimeError(f"no Argos package for {src}->{tgt}")
        pkg.install_from_path(p.download())

    langs = tr.get_installed_languages()
    from_lang = next(l for l in langs if l.code == src)
    to_lang = next(l for l in langs if l.code == tgt)
    translator = from_lang.get_translation(to_lang)
    return [translator.translate(t) for t in texts]
