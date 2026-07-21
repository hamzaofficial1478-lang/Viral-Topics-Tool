"""C1 — LLM hook detection via the task-routing provider layer (+ frame fusion).

Replaces the keyword heuristic that reported "0 strong standalone moments".

Mode (a), built here:
  * transcript scoring — the ``hook_detection`` task's LLM contributor (e.g.
    Forge ``gpt-luna-5.6``) scores every segment 0–1 against the shared rubric.
  * frame scoring — the vision contributor (``meta/llama-3.2-11b-vision-instruct``)
    rates the VISUAL interest of sampled frames for the top candidates.
  * fusion — the two scores combine into one ranking.

Mode (b) ``nemotron-omni`` on raw media and mode (c) all-three are deferred (they
need the omni adapter). Everything goes through the shared client + failover, so
a provider that errors cascades rather than silently producing nothing.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

from ..config import Config
from ..models import Candidate, Transcript
from ..utils import ShortForgeError, log
from . import rubric

_TASK = "hook_detection"


def _models_for(store: dict, category: str) -> list[dict]:
    """The hook_detection models of a category (LLM contributor / vision
    contributor), in bind/priority order — used as a per-role failover chain."""
    from ..providers.store import resolve_task
    return [m for m in resolve_task(store, _TASK) if m.get("category") == category]


def _json_obj(text: str) -> dict:
    """Parse a JSON object, tolerating code fences / prose around it."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", text or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return {}
    return {}


def score_transcript(transcript: Transcript, cfg: Config, store: dict, *,
                     batch: int = 80) -> dict[int, tuple[float, str]]:
    """Per-segment (score, reason) from the hook LLM. {} if none configured."""
    from ..providers import call_model_chat, run_failover
    chain = _models_for(store, "llm")
    if not chain:
        raise ShortForgeError(
            "Hook detection needs an LLM — bind one to the 'Hook detection' task in "
            "Settings → Task routing (e.g. Forge gpt-luna-5.6).")
    segs = transcript.segments
    out: dict[int, tuple[float, str]] = {}
    for base in range(0, len(segs), batch):
        chunk = segs[base:base + batch]
        listing = "\n".join(f"{base + i}\t[{s.start:.1f}-{s.end:.1f}] {s.text}"
                            for i, s in enumerate(chunk))
        prompt = (f"{rubric.LLM_RUBRIC}\n\nScore each numbered line 0.0 (weak) to 1.0 "
                  "(strong short-form hook). Return JSON "
                  '{"scores":[{"index":int,"score":number,"reason":string}]} — one entry '
                  "per line, reason ≤ 12 words.\n\nindex<TAB>[start-end] text:\n" + listing)
        content, model, fails = run_failover(chain, lambda m: call_model_chat(
            m, [{"role": "user", "content": prompt}], json_mode=True, max_tokens=1500))
        for f_m, err in fails:
            log.warning("hook LLM %s failed, cascaded: %s", f_m.get("model"), err)
        for s in _json_obj(content).get("scores", []):
            try:
                out[int(s["index"])] = (max(0.0, min(1.0, float(s["score"]))),
                                        str(s.get("reason", "")).strip())
            except (KeyError, ValueError, TypeError):
                continue
    return out


def score_frames_for(candidates: list[Candidate], indices: list[int], source_path: str,
                     cfg: Config, store: dict) -> dict[int, tuple[float, str]]:
    """Visual interest (score, reason) for the given candidate indices. {} if no
    vision contributor / no source."""
    chain = _models_for(store, "vision")
    if not chain or not source_path:
        return {}
    from ..providers import run_failover
    from ..providers.vision import sample_frames, score_frames
    n = int(cfg.get("vision.frames_per_clip", 6))
    fw = int(cfg.get("vision.frame_width", 768))
    max_imgs = int(cfg.get("vision.max_images", 6))
    tmp = tempfile.mkdtemp(prefix="hookframes_")
    prompt = ("Rate the VISUAL interest of these frames for a short-form clip from 0.0 to "
              "1.0 (facial expression intensity, motion, on-screen action, visual variety). "
              'Return JSON {"score":number,"reason":string} — reason ≤ 12 words.')
    out: dict[int, tuple[float, str]] = {}
    for idx in indices:
        c = candidates[idx]
        frames = sample_frames(source_path, c.start, c.end, n,
                               os.path.join(tmp, f"c{idx}"), fw)
        if not frames:
            continue

        def attempt(m):
            results = score_frames(m, frames, prompt, max_images=max_imgs)
            best = 0.0
            reason = ""
            for r in results:
                if r.get("status") != 200:
                    raise ShortForgeError(f"vision HTTP {r.get('status')}")
                try:
                    body = json.loads(r["body"])["choices"][0]["message"]["content"]
                except Exception as e:  # noqa: BLE001
                    raise ShortForgeError(f"bad vision response: {e}") from None
                d = _json_obj(body)
                sc = max(0.0, min(1.0, float(d.get("score", 0.0))))
                if sc >= best:
                    best, reason = sc, str(d.get("reason", "")).strip()
            return best, reason

        try:
            (score, reason), _, _ = run_failover(chain, attempt)
            out[idx] = (score, reason)
        except Exception as e:  # noqa: BLE001
            log.warning("frame scoring failed for candidate %d (%s)", idx, e)
    return out


def detect(transcript: Transcript, cfg: Config, source_path: str | None,
           store: dict) -> list[Candidate]:
    """Mode (a): transcript LLM scored, top candidates fused with frame scores."""
    tscores = score_transcript(transcript, cfg, store)
    cands: list[Candidate] = []
    for i, s in enumerate(transcript.segments):
        sc, reason = tscores.get(i, (0.0, ""))
        c = Candidate(start=s.start, end=s.end, score=sc, reason=reason)
        c.signals["transcript_llm"] = {"score": sc, "reason": reason}
        cands.append(c)

    fuse_frames = bool(cfg.get("detect.hook_frames", True))
    topk = int(cfg.get("detect.frame_topk", 12))
    if fuse_frames and source_path:
        order = sorted(range(len(cands)), key=lambda i: cands[i].score, reverse=True)[:topk]
        fscores = score_frames_for(cands, order, source_path, cfg, store)
        fw = float(cfg.get("detect.frame_weight", 0.35))
        for i, (vs, vreason) in fscores.items():
            cands[i].signals["frames_llm"] = {"score": vs, "reason": vreason}
            cands[i].score = round((1.0 - fw) * cands[i].score + fw * vs, 4)

    cands.sort(key=lambda c: c.score, reverse=True)
    return cands


def compare_modes(transcript: Transcript, cfg: Config, source_path: str | None,
                  store: dict) -> dict:
    """Candidates per mode for side-by-side evaluation (the `hooks` command).

    Modes: heuristic (baseline), transcript_llm (mode-a LLM only), fusion (mode a).
    Mode (b) omni / (c) all-three are deferred.
    """
    from . import heuristic
    modes: dict[str, list[Candidate]] = {}
    modes["heuristic"] = sorted(heuristic.detect(transcript, 0.0),
                                key=lambda c: c.score, reverse=True)
    tscores = score_transcript(transcript, cfg, store)
    tl = []
    for i, s in enumerate(transcript.segments):
        sc, r = tscores.get(i, (0.0, ""))
        tl.append(Candidate(start=s.start, end=s.end, score=sc, reason=r))
    modes["transcript_llm"] = sorted(tl, key=lambda c: c.score, reverse=True)
    modes["fusion"] = detect(transcript, cfg, source_path, store)
    return modes
