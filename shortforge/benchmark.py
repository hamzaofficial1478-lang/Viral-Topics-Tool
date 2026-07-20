"""STEP 2 — LLM benchmark.

Sends the identical prompt to every enabled LLM provider and compares them side
by side so the operator can choose the primary translator/analyst rather than
guessing. Two tasks:

- ``translate``: dub-translation of N real transcript segments; measures output
  length, estimated spoken duration vs the source segment's actual duration
  (flagging anything outside ±15%), latency, and estimated cost.
- ``hooks``: hook-detection over the same segments; shows each model's scored
  candidates + justifications.

Results are printed and written to a JSON + Markdown file.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .config import Config
from .cost import _CHARS_PER_SEC
from .detect import rubric
from .llm import anthropic_messages_url, normalize_chat_url, openai_chat_raw
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
        from .providers.store import load_store, providers_in
        store = load_store()
        seen = set()
        for cat in ("llm", "vision"):
            for p in providers_in(store, cat, enabled_only=True):
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
            return {"text": "", "latency_ms": (time.time()-t0)*1000, "status": None, "error": str(e)}
    extra = {"response_format": {"type": "json_object"}} if json_mode else None
    r = openai_chat_raw(entry.base_url, entry.api_key, entry.model, messages,
                        max_tokens=max_tokens, timeout=90, extra=extra)
    text = ""
    if r["status"] == 200:
        try:
            text = json.loads(r["body"])["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            text = r["body"][:500]
    return {"text": text, "latency_ms": (time.time()-t0)*1000,
            "status": r["status"], "error": r["error"] or (None if r["status"] == 200 else r["body"][:200])}


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


def render_markdown(results: dict) -> str:
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
