"""C1 — LLM hook detection (transcript scored via the task layer, frame fusion)."""

import json

import pytest

from shortforge.config import Config
from shortforge.models import Segment, Transcript
from shortforge.providers import store as S
from shortforge.detect import provider_hooks as PH
from shortforge.detect import detect_hooks


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "p.json"))
    # isolate the hook-score disk cache per test (no cross-test contamination)
    monkeypatch.setattr(PH, "_score_cache_path",
                        lambda cfg, tr, mid: str(tmp_path / "hookscores.json"))


def _tr():
    segs = [Segment(i * 4.0, i * 4.0 + 4.0, f"Segment {i} says something interesting.")
            for i in range(5)]
    return Transcript("en", 20.0, segs)


def _store_with_llm(vision=False):
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="Forge AI", base_url="https://www.forge-ai.space/v1",
                            api_key="fk")
    llm = S.add_model(store, cred["id"], model="gpt-5.6-luna", category="llm")
    binding = {"primary": llm["id"]}
    if vision:
        v = S.add_model(store, cred["id"], model="meta/llama-3.2-11b-vision-instruct",
                        category="vision", tier="free")
        binding["secondary"] = v["id"]
    S.set_task_binding(store, "hook_detection", **binding)
    S.save_store(store)
    return store


def _fake_chat(scores):
    body = json.dumps({"choices": [{"message": {"content": json.dumps({"scores": scores})}}]})
    return lambda base, key, model, messages, **kw: {
        "status": 200, "body": body, "error": None, "url": base, "model": model}


def test_score_transcript_parses_scores(monkeypatch):
    store = _store_with_llm()
    scores = [{"index": 0, "score": 0.9, "reason": "strong open loop"},
              {"index": 1, "score": 0.2, "reason": "filler"}]
    monkeypatch.setattr("shortforge.llm.openai_chat_raw", _fake_chat(scores))
    out = PH.score_transcript(_tr(), Config.load(), store)
    assert out[0] == (0.9, "strong open loop") and out[1][0] == 0.2


def test_score_transcript_requires_an_llm():
    with pytest.raises(Exception):
        PH.score_transcript(_tr(), Config.load(), {"providers": [], "credentials": [], "tasks": {}})


def test_detect_fuses_frame_scores(monkeypatch):
    store = _store_with_llm(vision=True)
    monkeypatch.setattr("shortforge.llm.openai_chat_raw",
                        _fake_chat([{"index": i, "score": 0.5, "reason": "ok"} for i in range(5)]))
    # frame scoring boosts candidate #2 (8.0–12.0s) without touching ffmpeg
    monkeypatch.setattr(PH, "score_frames_for",
                        lambda cands, idxs, src, cfg, store: {2: (1.0, "great visual")})
    cfg = Config.load()
    cfg.override("detect.hook_frames", True)     # vision fusion is off by default; this test exercises it
    cands = PH.detect(_tr(), cfg, "video.mp4", store)
    assert cands[0].start == 8.0                                   # fused to the top
    assert cands[0].signals["frames_llm"]["score"] == 1.0


def test_compare_modes_returns_three(monkeypatch):
    store = _store_with_llm()
    monkeypatch.setattr("shortforge.llm.openai_chat_raw",
                        _fake_chat([{"index": i, "score": 0.6, "reason": "r"} for i in range(5)]))
    modes = PH.compare_modes(_tr(), Config.load(), None, store)
    assert set(modes) == {"heuristic", "transcript_llm", "fusion"}
    assert len(modes["transcript_llm"]) == 5


def test_detect_hooks_routes_to_provider_when_bound(monkeypatch):
    _store_with_llm()
    monkeypatch.setattr("shortforge.llm.openai_chat_raw",
                        _fake_chat([{"index": i, "score": 0.8, "reason": "strong"} for i in range(5)]))
    cfg = Config.load()
    cfg.override("detect.min_segment_score", 0.0)
    cfg.override("detect.hook_frames", False)
    cands = detect_hooks(_tr(), cfg, None)
    assert cands and all(c.score == 0.8 for c in cands)           # LLM scores, not heuristic


def test_detect_hooks_falls_back_when_provider_errors(monkeypatch):
    _store_with_llm()
    monkeypatch.setattr("shortforge.llm.openai_chat_raw",
                        lambda *a, **k: {"status": 500, "body": "err", "error": None,
                                         "url": "", "model": "m"})
    cfg = Config.load()
    cfg.override("detect.min_segment_score", 0.0)
    cands = detect_hooks(_tr(), cfg, None)                         # must not raise
    assert isinstance(cands, list)                                # heuristic fallback ran
