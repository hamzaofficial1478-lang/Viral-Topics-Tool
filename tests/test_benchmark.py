"""STEP 2: benchmark-llm — segment picking, duration-fit flagging, rendering."""

from shortforge import benchmark as B
from shortforge.config import Config
from shortforge.models import Segment, Transcript


def _tr():
    segs = [Segment(i * 3.0, i * 3.0 + 3.0, f"Ceci est la phrase numero {i} du texte source.")
            for i in range(8)]
    return Transcript("fr", 24.0, segs)


def _providers():
    return [B.LLMEntry("nvidia", "https://x/v1", "k", "minimaxai/minimax-m3", "openai"),
            B.LLMEntry("vercel", "https://y/v1", "k", "anthropic/claude", "openai")]


def test_candidate_entries_reuse_borrowed_creds():
    borrow = B.LLMEntry("nvidia", "https://integrate.api.nvidia.com/v1", "nvapi-k",
                        "minimaxai/minimax-m3", "openai")
    ents = B.candidate_entries(" minimaxai/minimax-m3 , z-ai/glm-5.2 ,", borrow)
    assert [e.model for e in ents] == ["minimaxai/minimax-m3", "z-ai/glm-5.2"]
    assert [e.name for e in ents] == ["minimax-m3", "glm-5.2"]          # short, readable
    assert all(e.base_url == borrow.base_url and e.api_key == borrow.api_key
               and e.api_shape == "openai" for e in ents)
    assert B.candidate_entries("", borrow) == []


def test_pick_segments_spreads():
    segs = B.pick_segments(_tr(), 4)
    assert len(segs) == 4
    # spread across the source, not the first four
    assert segs[0].start == 0.0 and segs[-1].start >= 12.0


def test_translate_flags_duration_drift(monkeypatch):
    # source segments are 3.0s => 42 chars ~= 3.0s at 14 chars/s (in tolerance).
    # Provider A returns ~42 chars (fits); provider B returns a long line (fails).
    def fake_call(entry, prompt, *, max_tokens=400, json_mode=False):
        if entry.name == "nvidia":
            return {"text": "x" * 42, "latency_ms": 120, "status": 200, "error": None}
        return {"text": "y" * 120, "latency_ms": 300, "status": 200, "error": None}
    monkeypatch.setattr(B, "_call", fake_call)

    res = B.run_translate(_tr(), "en", _providers(), 3, Config.load())
    assert res["task"] == "translate" and len(res["segments"]) == 3
    r0 = {r["provider"]: r for r in res["segments"][0]["results"]}
    assert r0["nvidia"]["within_tol"] is True
    assert r0["vercel"]["within_tol"] is False           # 120 chars ~8.6s vs 3s
    assert r0["vercel"]["drift_pct"] > 15


def test_translate_records_errors(monkeypatch):
    def fake_call(entry, prompt, *, max_tokens=400, json_mode=False):
        return {"text": "", "latency_ms": 90, "status": 401, "error": "invalid key"}
    monkeypatch.setattr(B, "_call", fake_call)
    res = B.run_translate(_tr(), "en", _providers()[:1], 1, Config.load())
    r = res["segments"][0]["results"][0]
    assert r["status"] == 401 and r["error"] == "invalid key"


def test_hooks_parses_scores(monkeypatch):
    import json
    def fake_call(entry, prompt, *, max_tokens=400, json_mode=False):
        return {"text": json.dumps({"scores": [
            {"index": 2, "score": 0.9, "reason": "strong claim"},
            {"index": 0, "score": 0.4, "reason": "weak"}]}),
            "latency_ms": 200, "status": 200, "error": None}
    monkeypatch.setattr(B, "_call", fake_call)
    res = B.run_hooks(_tr(), _providers()[:1], 4, Config.load())
    top = res["providers"][0]["top"]
    assert top[0]["index"] == 2 and top[0]["score"] == 0.9   # sorted by score desc


def test_render_markdown_translate(monkeypatch):
    def fake_call(entry, prompt, *, max_tokens=400, json_mode=False):
        return {"text": "Hello there friend", "latency_ms": 100, "status": 200, "error": None}
    monkeypatch.setattr(B, "_call", fake_call)
    res = B.run_translate(_tr(), "en", _providers()[:1], 2, Config.load())
    md = B.render_markdown(res)
    assert "LLM benchmark" in md and "| Provider |" in md and "nvidia" in md


def test_enabled_llms_from_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "p.json"))
    from shortforge.providers import store as S
    store = {"providers": []}
    p = S.add_provider(store, name="NVIDIA", category="llm",
                       base_url="https://integrate.api.nvidia.com/v1", api_key="k",
                       model="z-ai/glm-5.2")
    S.update_provider(store, p["id"], api_shape="openai")
    S.save_store(store)
    llms = B.enabled_llms(Config.load())
    assert any(e.name == "NVIDIA" and e.model == "z-ai/glm-5.2" for e in llms)
