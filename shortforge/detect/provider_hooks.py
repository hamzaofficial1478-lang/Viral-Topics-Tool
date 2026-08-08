"""C1 — LLM hook detection via the task-routing provider layer (+ frame fusion).

Replaces the keyword heuristic that reported "0 strong standalone moments".

Mode (a), built here:
  * transcript scoring — the ``hook_detection`` task's LLM contributor
    (``minimaxai/minimax-m3``) scores every segment 0–1 against the shared rubric.
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
RUBRIC_VERSION = "1"    # bump to invalidate cached hook scores when the prompt changes


def _score_cache_path(cfg: Config, transcript: Transcript, model_id: str) -> str:
    """Disk cache key: (transcript content, model, rubric version) — so re-runs and
    the fusion/transcript modes never re-pay for the same scoring (Blocker 1).

    This is content-addressed on the transcript rather than the raw source hash on
    purpose: a given source always yields the same transcript, so it's equivalent
    to keying on ``source_hash`` for re-runs — but it *also* invalidates correctly
    if the operator re-transcribes with a different Whisper model (the segments,
    hence the scores, change), which a bare source hash would miss (silent-stale)."""
    import hashlib
    h = hashlib.sha1()
    for s in transcript.segments:
        h.update(f"{s.start:.2f}|{s.text}\n".encode("utf-8"))
    h.update(f"{model_id}|{RUBRIC_VERSION}".encode("utf-8"))
    work = str(cfg.get("paths.work_dir", ".shortforge"))
    return os.path.join(work, "hookscores", h.hexdigest()[:16] + ".json")


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


def _is_timeout_error(exc: Exception) -> bool:
    """A failed batch was a timeout (worth a smaller retry) vs a hard error (4xx,
    bad response) that a smaller request won't fix. call_model_chat's timeout
    message says 'timed out after Ns'; run_failover wraps it verbatim."""
    return "timed out" in str(exc).lower()


def score_transcript(transcript: Transcript, cfg: Config, store: dict,
                     batch: int | None = None) -> dict[int, tuple[float, str]]:
    """Per-segment (score, reason) from the hook LLM, scored in small chunks.

    Reliability (Blocker 1):
      * The transcript is split into ``detect.hook_batch`` (~12) segment chunks —
        small, predictable calls finish under the timeout far more reliably than a
        few large ones.
      * Chunks run CONCURRENTLY (``detect.hook_concurrency``, default 4).
      * On a *timeout* (after per-call retries), a chunk is retried at HALF the
        size — recursively, down to a single segment — instead of re-sending the
        same oversized request.
      * If a chunk still can't be scored, the run CONTINUES with the chunks that
        succeeded and the unscored segments are reported loudly (never a whole-run
        failure). A partial result is NOT cached, so a re-run retries the gaps.

    Scores merge into one global index → (score, reason) map.
    """
    from ..providers import call_model_chat, run_failover
    from ..providers.store import task_timeout
    chain = _models_for(store, "llm")
    if not chain:
        raise ShortForgeError(
            "Hook detection needs an LLM — bind one to the 'Hook detection' task in "
            "Settings → Task routing (e.g. minimaxai/minimax-m3).")
    segs = transcript.segments

    # issue 3: reuse cached scores (same transcript + model + rubric) instead of
    # re-paying — the transcript_llm and fusion modes, and any re-run, share them.
    model_id = chain[0].get("model", "?")
    cache_path = _score_cache_path(cfg, transcript, model_id)
    if not cfg.get("cache.disabled", False):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            log.info("hook scores: cache hit (%d segments) — no LLM call", len(data))
            return {int(k): (float(v[0]), str(v[1])) for k, v in data.items()}
        except (FileNotFoundError, ValueError, KeyError):
            pass

    batch = int(batch or cfg.get("detect.hook_batch", 12) or 12)
    timeout = int(cfg.get("detect.hook_timeout", 0) or task_timeout("hook_detection"))
    retries = int(cfg.get("providers.retries", 2))
    workers = max(1, int(cfg.get("detect.hook_concurrency", 4) or 1))
    starts = list(range(0, len(segs), batch))
    batches = [list(range(b, min(b + batch, len(segs)))) for b in starts]
    n_batches = len(batches)
    log.info("hook scoring: %d segment(s) in %d batch(es) of %d (timeout %ds, %d retries, "
             "%d parallel)", len(segs), n_batches, batch, timeout, retries, min(workers, n_batches))

    def _score_indices(idxs: list[int]) -> tuple[dict[int, tuple[float, str]], list[int]]:
        """Score the given global segment indices. On timeout, split in half and
        retry each half (a smaller request) down to one segment. Returns
        (scores, unscored_indices) and NEVER raises — one dead chunk can't fail
        the run."""
        listing = "\n".join(f"{i}\t[{segs[i].start:.1f}-{segs[i].end:.1f}] {segs[i].text}"
                            for i in idxs)
        prompt = (f"{rubric.LLM_RUBRIC}\n\nScore each numbered line 0.0 (weak) to 1.0 "
                  "(strong short-form hook). Return JSON "
                  '{"scores":[{"index":int,"score":number,"reason":string}]} — one entry '
                  "per line, reason ≤ 12 words.\n\nindex<TAB>[start-end] text:\n" + listing)
        try:
            content, _model, fails = run_failover(chain, lambda m: call_model_chat(
                m, [{"role": "user", "content": prompt}], json_mode=True, max_tokens=1500,
                timeout=timeout, retries=retries))
        except ShortForgeError as e:
            if _is_timeout_error(e) and len(idxs) > 1:
                mid = len(idxs) // 2
                log.warning("hook chunk of %d segment(s) timed out — retrying as %d + %d "
                            "(half size)", len(idxs), mid, len(idxs) - mid)
                left_s, left_u = _score_indices(idxs[:mid])
                right_s, right_u = _score_indices(idxs[mid:])
                left_s.update(right_s)
                return left_s, left_u + right_u
            log.warning("hook chunk (segments %d-%d) could not be scored after retries — "
                        "skipping: %s", idxs[0], idxs[-1], e)
            return {}, list(idxs)
        for f_m, err in fails:
            log.warning("hook LLM %s failed, cascaded: %s", f_m.get("model"), err)
        res: dict[int, tuple[float, str]] = {}
        for s in _json_obj(content).get("scores", []):
            try:
                gi = int(s["index"])
                if gi in idxs:                      # ignore stray indices from the model
                    res[gi] = (max(0.0, min(1.0, float(s["score"]))),
                               str(s.get("reason", "")).strip())
            except (KeyError, ValueError, TypeError):
                continue
        return res, []

    def _do_batch(bi: int, idxs: list[int]) -> tuple[dict[int, tuple[float, str]], list[int]]:
        log.info("hook batch %d/%d (%d segments)…", bi, n_batches, len(idxs))
        return _score_indices(idxs)

    out: dict[int, tuple[float, str]] = {}
    unscored: list[int] = []
    work = list(enumerate(batches, 1))
    # issue 4: run the batches concurrently (4 in flight × ~150s ≈ well under 60 RPM).
    if workers > 1 and n_batches > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, n_batches)) as ex:
            results = list(ex.map(lambda t: _do_batch(t[0], t[1]), work))
    else:
        results = [_do_batch(bi, idxs) for bi, idxs in work]
    for res, miss in results:
        out.update(res)
        unscored.extend(miss)

    # Final re-sweep: chunks usually drop because the provider throttles or resets
    # the socket under concurrent load (e.g. WinError 10054). Once the parallel
    # batches are done that pressure is gone, so retry the gaps ONE more time,
    # sequentially and in small pieces — this recovers most losses instead of
    # leaving a minute of the video unscored.
    if unscored:
        gaps = sorted(set(unscored))
        log.info("hook scoring: re-sweeping %d unscored segment(s) sequentially "
                 "(provider load has settled)…", len(gaps))
        recovered: dict[int, tuple[float, str]] = {}
        still: list[int] = []
        # Genuinely smaller than the batch that just failed — a re-sweep at the
        # same size would re-send the same request that the provider dropped.
        sweep = max(1, min(batch // 2, 4))
        for i in range(0, len(gaps), sweep):
            piece = gaps[i:i + sweep]
            res, miss = _score_indices(piece)
            recovered.update(res)
            still.extend(miss)
        if recovered:
            out.update(recovered)
            log.info("hook scoring: re-sweep recovered %d of %d segment(s)",
                     len(recovered), len(gaps))
        unscored = still

    if unscored:
        unscored.sort()
        shown = ", ".join(f"{i} [{segs[i].start:.1f}-{segs[i].end:.1f}s]" for i in unscored[:12])
        more = "" if len(unscored) <= 12 else f" … (+{len(unscored) - 12} more)"
        log.warning("hook scoring: %d of %d segment(s) could NOT be scored after retries, "
                    "half-size splits and a re-sweep — CONTINUING with %d scored. "
                    "Unscored: %s%s", len(unscored), len(segs), len(out), shown, more)

    # Cache only a COMPLETE result — never a partial one, so a re-run retries the
    # gaps instead of returning them from cache.
    if not cfg.get("cache.disabled", False) and out and not unscored:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({str(k): [v[0], v[1]] for k, v in out.items()}, f)
        except OSError:
            pass
    return out


def score_frames_for(candidates: list[Candidate], indices: list[int], source_path: str,
                     cfg: Config, store: dict) -> dict[int, tuple[float, str]]:
    """Visual interest (score, reason) for the given candidate indices. {} if no
    vision contributor / no source."""
    chain = _models_for(store, "vision")
    if not chain or not source_path:
        return {}
    from ..providers import run_failover
    from ..providers.store import task_timeout
    from ..providers.vision import sample_frames, score_frames
    n = int(cfg.get("vision.frames_per_clip", 6))
    fw = int(cfg.get("vision.frame_width", 768))
    max_imgs = int(cfg.get("vision.max_images", 6))
    vtimeout = int(cfg.get("vision.timeout", 0) or task_timeout("vision_scoring"))
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
            results = score_frames(m, frames, prompt, max_images=max_imgs, timeout=vtimeout)
            best = 0.0
            reason = ""
            for r in results:
                if r.get("status") != 200:
                    # issue 2: surface the FULL response body so a 400 is diagnosable
                    # (encoding? too many images? resolution? payload size?). Redacted:
                    # a gateway error page can echo back the request, key included.
                    from ..providers.base import redact
                    body = redact((r.get("body") or r.get("error") or "")[:600],
                                 m.get("api_key"))
                    raise ShortForgeError(
                        f"vision HTTP {r.get('status')} ({r.get('images')} image(s), "
                        f"max_images={max_imgs}, frame_width={fw}px): {body} "
                        f"— if it's a count/size cap, lower vision.max_images (try 1) "
                        f"and/or vision.frame_width, then raise to the real limit.")
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
           store: dict, tscores: dict | None = None) -> list[Candidate]:
    """Mode (a): transcript LLM scored, top candidates fused with frame scores.

    ``tscores`` may be passed in to reuse an already-computed scoring (issue 3)."""
    tscores = tscores if tscores is not None else score_transcript(transcript, cfg, store)
    cands: list[Candidate] = []
    for i, s in enumerate(transcript.segments):
        sc, reason = tscores.get(i, (0.0, ""))
        c = Candidate(start=s.start, end=s.end, score=sc, reason=reason)
        c.signals["transcript_llm"] = {"score": sc, "reason": reason}
        cands.append(c)

    fuse_frames = bool(cfg.get("detect.hook_frames", False))
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
    tscores = score_transcript(transcript, cfg, store)      # issue 3: scored ONCE
    tl = []
    for i, s in enumerate(transcript.segments):
        sc, r = tscores.get(i, (0.0, ""))
        tl.append(Candidate(start=s.start, end=s.end, score=sc, reason=r))
    modes["transcript_llm"] = sorted(tl, key=lambda c: c.score, reverse=True)
    modes["fusion"] = detect(transcript, cfg, source_path, store, tscores=tscores)  # reuse
    return modes
