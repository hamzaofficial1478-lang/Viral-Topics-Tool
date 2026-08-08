"""STEP 2 / 3.5b — provider benchmark.

Runs the identical work through every configured provider and compares them side
by side so the operator can choose rather than guess. Tasks:

- ``translate``: dub-translation of N real transcript segments; measures output
  length, estimated spoken duration vs the source segment's actual duration
  (flagging anything outside ±15%), latency, and estimated cost.
- ``hooks``: hook-detection over the same segments; shows each model's scored
  candidates + justifications.
- ``transcribe`` (STEP 3.5b): the same audio through every configured ASR
  backend (local Whisper + any API), showing quality indicators (mean
  confidence, low-confidence segments, and text divergence vs the strongest
  backend — there is no ground truth, so this stands in for a word-error rate),
  latency, and cost. Lets the operator hear/read which backend garbles their
  footage before it poisons every downstream translation.

Results are printed and written to a JSON + Markdown file.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass

from .config import Config
from .cost import _CHARS_PER_SEC
from .detect import rubric
from .llm import anthropic_messages_url, openai_chat_raw
from .models import Transcript
from .utils import log

_DUR_TOLERANCE = 0.15


@dataclass
class LLMEntry:
    name: str
    base_url: str
    api_key: str
    model: str
    api_shape: str


def enabled_llms(cfg: Config) -> list[LLMEntry]:
    """Every enabled LLM/vision provider from the settings store; env fallback."""
    out: list[LLMEntry] = []
    try:
        from .providers.store import load_store, models_in
        store = load_store()
        seen = set()
        for cat in ("llm", "vision"):
            for p in models_in(store, cat, enabled_only=True):
                key = (p.get("base_url"), p.get("model"), p.get("api_key"))
                if key in seen or not p.get("api_key"):
                    continue
                seen.add(key)
                out.append(LLMEntry(p.get("name", "llm"), p.get("base_url", ""),
                                    p.get("api_key", ""), p.get("model", ""),
                                    p.get("api_shape", "openai")))
    except Exception as e:  # noqa: BLE001
        log.warning("could not read provider store (%s)", e)
    if out:
        return out
    # Fallback: the single env/Part-E configured provider.
    from .llm import resolve
    c = resolve()
    if c.provider != "none" and c.api_key:
        out.append(LLMEntry(c.provider, c.base_url or "", c.api_key, c.model, c.provider))
    return out


def candidate_entries(models: str, borrow: LLMEntry) -> list[LLMEntry]:
    """Comma-separated model ids → benchmark entries that reuse ``borrow``'s
    endpoint + key, so candidate models need no settings-UI provider row.

    The short name is the model's last path segment (``minimaxai/minimax-m3`` →
    ``minimax-m3``) to keep the results table readable.
    """
    out: list[LLMEntry] = []
    for m in (models or "").split(","):
        m = m.strip()
        if m:
            out.append(LLMEntry(m.split("/")[-1], borrow.base_url, borrow.api_key,
                                m, borrow.api_shape))
    return out


def _call(entry: LLMEntry, prompt: str, *, max_tokens: int = 400,
          json_mode: bool = False) -> dict:
    """One LLM call, timed. Returns {text, latency_ms, status, error}."""
    messages = [{"role": "user", "content": prompt}]
    t0 = time.time()
    if entry.api_shape == "anthropic":
        import urllib.error
        import urllib.request
        payload = {"model": entry.model, "max_tokens": max_tokens, "messages": messages}
        try:
            req = urllib.request.Request(
                anthropic_messages_url(entry.base_url), data=json.dumps(payload).encode(),
                headers={"x-api-key": entry.api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read().decode("utf-8", "replace")
            blocks = json.loads(body).get("content", [])
            text = next((b.get("text", "") for b in blocks if b.get("type") == "text"), "")
            return {"text": text, "latency_ms": (time.time()-t0)*1000, "status": 200, "error": None}
        except Exception as e:  # noqa: BLE001
            from .providers.base import redact
            return {"text": "", "latency_ms": (time.time()-t0)*1000, "status": None,
                    "error": redact(str(e), entry.api_key)}
    extra = {"response_format": {"type": "json_object"}} if json_mode else None
    r = openai_chat_raw(entry.base_url, entry.api_key, entry.model, messages,
                        max_tokens=max_tokens, timeout=90, extra=extra)
    text = ""
    if r["status"] == 200:
        try:
            text = json.loads(r["body"])["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            text = r["body"][:500]
    from .providers.base import redact
    err = r["error"] or (None if r["status"] == 200 else r["body"][:200])
    return {"text": text, "latency_ms": (time.time()-t0)*1000,
            "status": r["status"], "error": redact(err, entry.api_key) if err else err}


def pick_segments(transcript: Transcript, n: int) -> list:
    """N substantial segments spread across the source."""
    segs = [s for s in transcript.segments if len((s.text or "").strip()) >= 20]
    if not segs:
        segs = list(transcript.segments)
    if len(segs) <= n:
        return segs
    step = len(segs) / n
    return [segs[int(i * step)] for i in range(n)]


def _cost(entry_caps_cost: float, chars: int) -> float | None:
    return round(chars / 1000.0 * entry_caps_cost, 4) if entry_caps_cost else None


def run_translate(transcript: Transcript, target_lang: str, providers: list[LLMEntry],
                  n: int, cfg: Config) -> dict:
    src_lang = transcript.language or "auto"
    segments = pick_segments(transcript, n)
    rows = []
    for seg in segments:
        actual = max(0.1, seg.end - seg.start)
        prompt = (
            f"You are dubbing a short-form video from {src_lang} to {target_lang}. "
            f"Translate the line below into natural SPOKEN {target_lang} (contractions, "
            f"idiom — what a native speaker would actually say aloud, not a literal "
            f"rendering). It must be speakable in about {actual:.1f} seconds. "
            f"Return ONLY the translation.\n\nLine: {seg.text}"
        )
        seg_rows = []
        for p in providers:
            res = _call(p, prompt, max_tokens=300)
            text = (res["text"] or "").strip()
            chars = len(text)
            est = chars / _CHARS_PER_SEC
            drift = (est - actual) / actual if actual else 0.0
            seg_rows.append({
                "provider": p.name, "model": p.model, "output": text, "chars": chars,
                "est_spoken_s": round(est, 1), "source_s": round(actual, 1),
                "drift_pct": round(drift * 100, 1),
                "within_tol": abs(drift) <= _DUR_TOLERANCE,
                "latency_ms": round(res["latency_ms"]),
                "status": res["status"], "error": res["error"],
            })
        rows.append({"source_text": seg.text, "source_s": round(actual, 1), "results": seg_rows})
    return {"task": "translate", "src_lang": src_lang, "target_lang": target_lang, "segments": rows}


def run_hooks(transcript: Transcript, providers: list[LLMEntry], n: int, cfg: Config) -> dict:
    segments = pick_segments(transcript, n)
    listing = "\n".join(f"{i}\t[{s.start:.1f}-{s.end:.1f}] {s.text}"
                        for i, s in enumerate(segments))
    prompt = (
        f"{rubric.LLM_RUBRIC}\n\n"
        "Score each numbered line below from 0.0 (weak) to 1.0 (strong short-form "
        "hook). Return JSON: {\"scores\":[{\"index\":int,\"score\":number,"
        "\"reason\":string}]}.\n\nindex<TAB>[start-end] text:\n" + listing
    )
    rows = []
    for p in providers:
        res = _call(p, prompt, max_tokens=1200, json_mode=True)
        scores = []
        try:
            data = json.loads(res["text"])
            scores = sorted(data.get("scores", []), key=lambda x: -float(x.get("score", 0)))[:5]
        except Exception:  # noqa: BLE001
            pass
        rows.append({"provider": p.name, "model": p.model,
                     "latency_ms": round(res["latency_ms"]), "status": res["status"],
                     "error": res["error"], "top": scores})
    return {"task": "hooks", "segments": [s.text for s in segments], "providers": rows}


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _asr_backends(cfg: Config) -> list:
    """Available ASR backends, de-duplicated (store providers + local Whisper)."""
    from .providers.asr import list_asr_providers
    seen: set = set()
    out = []
    for p in list_asr_providers(cfg):
        if not p.available():
            continue
        key = (type(p).__name__, getattr(p, "model", getattr(p, "model_size", "")),
               getattr(p, "base_url", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def run_transcribe(audio_path: str, cfg: Config, *, language: str | None = None,
                   duration: float = 0.0) -> dict:
    """Transcribe the same audio through every configured ASR backend and compare.

    Quality indicators: mean per-segment confidence, count of low-confidence
    segments, and text divergence vs the strongest backend (a stand-in for a
    word-error rate — there is no reference transcript). Also latency and cost.
    """
    backends = _asr_backends(cfg)
    dur_min = max(0.0, (duration or 0.0)) / 60.0
    rows = []
    for b in backends:
        t0 = time.time()
        err = None
        transcript = None
        try:
            transcript = b.transcribe(audio_path, language=language, duration=duration)
        except Exception as e:  # noqa: BLE001
            err = str(e)
        latency = round((time.time() - t0) * 1000)
        if transcript is None:
            rows.append({"provider": b.name, "model": getattr(b, "model", getattr(b, "model_size", "")),
                         "error": err, "latency_ms": latency, "segments": 0, "words": 0,
                         "mean_conf": None, "low_conf": 0, "text": "", "cost": None})
            continue
        confs = [s.confidence for s in transcript.segments if s.confidence is not None]
        mean_conf = round(sum(confs) / len(confs), 3) if confs else None
        low = sum(1 for c in confs if c < 0.35)
        text = " ".join(s.text for s in transcript.segments).strip()
        rate = getattr(b, "cost_per_min", 0.0) or 0.0
        cost = round(dur_min * rate, 4) if rate else (0.0 if type(b).__name__ == "LocalWhisperASR" else None)
        rows.append({"provider": b.name, "model": getattr(b, "model", getattr(b, "model_size", "")),
                     "error": None, "latency_ms": latency, "segments": len(transcript.segments),
                     "words": len(transcript.words()), "mean_conf": mean_conf, "low_conf": low,
                     "text": text, "cost": cost, "lang": transcript.language})

    # Reference = strongest successful backend (highest mean confidence, else first).
    ok = [r for r in rows if r["error"] is None and r["text"]]
    ref = max(ok, key=lambda r: (r["mean_conf"] is not None, r["mean_conf"] or 0.0), default=None)
    ref_norm = _norm_text(ref["text"]) if ref else ""
    for r in rows:
        if r is ref:
            r["divergence_pct"] = 0.0
        elif r["text"] and ref_norm:
            ratio = difflib.SequenceMatcher(None, _norm_text(r["text"]), ref_norm).ratio()
            r["divergence_pct"] = round((1.0 - ratio) * 100, 1)
        else:
            r["divergence_pct"] = None
    return {"task": "transcribe", "language": language or (ref.get("lang") if ref else "auto"),
            "audio": audio_path, "duration_s": round(duration or 0.0, 1),
            "reference": ref["provider"] if ref else None, "backends": rows}


def render_markdown(results: dict) -> str:
    if results["task"] == "transcribe":
        lines = [f"# ASR benchmark — {results['task']}", ""]
        lines.append(f"Audio: `{results['audio']}`  ({results['duration_s']}s)  "
                     f"— reference backend: **{results['reference'] or 'n/a'}**")
        lines.append("_Divergence = text distance vs the reference backend; there is no "
                     "ground truth, so treat it as a relative word-error signal._\n")
        lines.append("| Backend | Model | Segs | Words | Mean conf | Low-conf | Divergence | Latency | Cost |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in results["backends"]:
            if r["error"]:
                lines.append(f"| {r['provider']} | {r['model']} | ⚠ {r['error'][:60]} |||||||")
                continue
            mc = f"{r['mean_conf']:.2f}" if r["mean_conf"] is not None else "—"
            dv = f"{r['divergence_pct']}%" if r["divergence_pct"] is not None else "—"
            cost = f"${r['cost']:g}" if r["cost"] is not None else "n/a"
            lines.append(f"| {r['provider']} | {r['model']} | {r['segments']} | {r['words']} | "
                         f"{mc} | {r['low_conf']} | {dv} | {r['latency_ms']}ms | {cost} |")
        lines.append("")
        for r in results["backends"]:
            if r["error"] or not r["text"]:
                continue
            lines.append(f"### {r['provider']} — sample")
            lines.append(f"> {r['text'][:400]}{'…' if len(r['text']) > 400 else ''}\n")
        return "\n".join(lines)

    lines = [f"# LLM benchmark — {results['task']}", ""]
    if results["task"] == "translate":
        lines.append(f"{results['src_lang']} → {results['target_lang']}  "
                     f"(±{int(_DUR_TOLERANCE*100)}% duration target)\n")
        for i, seg in enumerate(results["segments"], 1):
            lines.append(f"## Segment {i} — source {seg['source_s']}s")
            lines.append(f"> {seg['source_text']}\n")
            lines.append("| Provider | Model | Output | Chars | Est/Src (s) | Drift | Latency | Fit |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for r in seg["results"]:
                out = (r["output"][:80] + "…") if len(r["output"]) > 80 else (r["output"] or f"⚠ {r.get('error','')}")
                fit = "✓" if r["within_tol"] else "✗"
                lines.append(f"| {r['provider']} | {r['model']} | {out} | {r['chars']} | "
                             f"{r['est_spoken_s']}/{r['source_s']} | {r['drift_pct']:+}% | "
                             f"{r['latency_ms']}ms | {fit} |")
            lines.append("")
    else:
        lines.append("Hook detection over " + str(len(results["segments"])) + " segments\n")
        for r in results["providers"]:
            lines.append(f"## {r['provider']} — {r['model']}  ({r['latency_ms']}ms)")
            if r.get("error"):
                lines.append(f"⚠ {r['error']}")
            for s in r.get("top", []):
                lines.append(f"- [{s.get('score')}] #{s.get('index')} — {s.get('reason','')}")
            lines.append("")
    return "\n".join(lines)
