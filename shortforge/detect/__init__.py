"""M3 — Hook / highlight detection.

Scores each transcript segment for hook strength, then (M3++) fuses a *visual*
score — motion, scene cuts, face presence — so the "best parts" reflect what's
shown, not just what's said. Transcript scoring uses Claude when available,
otherwise a keyword heuristic.
"""

from __future__ import annotations


from ..config import Config
from ..models import Candidate, Transcript
from ..utils import log
from . import heuristic, visual


def _llm_available() -> bool:
    from ..llm import available
    return available()


def _use_provider_hooks(cfg: Config) -> bool:
    """C1: use the task-routing LLM+frame scorer when forced, or (auto) when a
    hook_detection LLM is bound in the settings store."""
    backend = cfg.get("detect.backend", "auto")
    if backend in ("provider", "fusion"):
        return True
    if backend == "auto":
        try:
            from ..providers.store import load_store, resolve_task
            return any(m.get("category") == "llm"
                       for m in resolve_task(load_store(), "hook_detection"))
        except Exception:  # noqa: BLE001
            return False
    return False


def _transcript_candidates(
    transcript: Transcript, cfg: Config, source_path: str | None
) -> tuple[list[Candidate], str]:
    """Score every segment (unfiltered) + backend name.

    Prefers the Claude-vision multimodal scorer when opted in and available,
    then the Claude transcript scorer, then the keyword heuristic.
    """
    if source_path and cfg.get("detect.vision_llm", False):
        from . import vision_llm

        if vision_llm.available():
            try:
                return vision_llm.detect(transcript, source_path, cfg), "claude-vision"
            except Exception as e:  # noqa: BLE001
                log.warning("vision scorer failed (%s); falling back", e)

    backend = cfg.get("detect.backend", "auto")
    if backend == "llm" or (backend == "auto" and _llm_available()):
        try:
            from . import llm

            return llm.detect(transcript, cfg), "claude"
        except Exception as e:  # noqa: BLE001
            log.warning("LLM hook detection unavailable (%s); using heuristic", e)
    return heuristic.detect(transcript, 0.0), "heuristic"


def detect_hooks(
    transcript: Transcript, cfg: Config, source_path: str | None = None
) -> list[Candidate]:
    """Return ranked hook candidates, fusing transcript + visual signals."""
    # --- C1: LLM hook scorer (transcript LLM + vision frame fusion) ---------- #
    if _use_provider_hooks(cfg):
        from ..providers.store import load_store
        from . import provider_hooks
        try:
            cands = provider_hooks.detect(transcript, cfg, source_path, load_store())
            min_score = float(cfg.get("detect.min_segment_score", 0.0))
            filtered = [c for c in cands if c.score >= min_score]
            strong = sum(1 for c in filtered if c.score >= 0.5)
            log.info("hook detection: provider-llm+frames (%d/%d above %.2f, %d strong)",
                     len(filtered), len(cands), min_score, strong)
            return filtered
        except Exception as e:  # noqa: BLE001 - visible fallback, never abort the run
            log.warning("provider hook detection failed (%s); falling back to heuristic", e)

    candidates, backend = _transcript_candidates(transcript, cfg, source_path)

    # --- M3++ visual fusion ------------------------------------------------ #
    want_visual = source_path and cfg.get("detect.visual", True) and visual.available()
    if want_visual:
        vs = visual.analyze(source_path, transcript.duration, cfg)
        if vs:
            vw = float(cfg.get("detect.visual_weight", 0.35))
            for c in candidates:
                vscore, vsig = vs.score_window(c.start, c.end, cfg)
                if vsig:
                    c.signals["visual"] = {**vsig, "score": round(vscore, 3)}
                    c.score = round((1.0 - vw) * c.score + vw * vscore, 4)
            candidates.sort(key=lambda c: c.score, reverse=True)
            backend += "+visual"

    min_score = float(cfg.get("detect.min_segment_score", 0.0))
    filtered = [c for c in candidates if c.score >= min_score]
    log.info(
        "hook detection: %s (%d/%d segments above %.2f)",
        backend, len(filtered), len(candidates), min_score,
    )
    return filtered


__all__ = ["detect_hooks"]
