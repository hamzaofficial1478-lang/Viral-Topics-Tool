"""C1 follow-ups: hook-score caching (dedup), concurrency merge, clip-level
selection (hook early), and vision 400 diagnostics."""

import json

import pytest

from shortforge.config import Config
from shortforge.models import Segment, Transcript
from shortforge.providers import store as S
from shortforge.detect import provider_hooks as PH
from shortforge.select import build_clips


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "p.json"))


def _store():
    st = {"providers": [], "credentials": [], "tasks": {}}
    c = S.add_credential(st, name="NVIDIA build", base_url="https://integrate.api.nvidia.com/v1",
                         api_key="nvapi-x")
    m = S.add_model(st, c["id"], model="minimaxai/minimax-m3", category="llm")
    S.set_task_binding(st, "hook_detection", primary=m["id"])
    S.save_store(st)
    return st


def _tr(n=6):
    return Transcript("fr", n * 4.0, [Segment(i * 4.0, i * 4.0 + 4.0, f"segment {i} text") for i in range(n)])


def _fake_scored():
    import re

    def fake(base, key, model, messages, **kw):
        idxs = [int(x) for x in re.findall(r"(?m)^(\d+)\t", messages[0]["content"])]
        scores = [{"index": i, "score": 0.6, "reason": "hook"} for i in idxs]
        return {"status": 200,
                "body": json.dumps({"choices": [{"message": {"content": json.dumps({"scores": scores})}}]}),
                "error": None, "url": base, "model": model, "auth": "Authorization"}
    return fake


# --- issue 3: caching + no double pass -------------------------------------- #

def test_scores_cached_and_reused(tmp_path, monkeypatch):
    calls = {"c": 0}

    def fake(*a, **k):
        calls["c"] += 1
        return _fake_scored()(*a, **k)

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    out1 = PH.score_transcript(_tr(), cfg, _store())
    after_first = calls["c"]
    assert after_first >= 1
    out2 = PH.score_transcript(_tr(), cfg, _store())            # identical → cache hit
    assert calls["c"] == after_first and out1 == out2           # no new LLM calls


def test_compare_modes_scores_once(tmp_path, monkeypatch):
    calls = {"c": 0}

    def fake(*a, **k):
        calls["c"] += 1
        return _fake_scored()(*a, **k)

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    PH.compare_modes(_tr(6), cfg, None, _store())              # 6 segs / batch 25 = 1 call
    assert calls["c"] == 1                                     # transcript_llm + fusion reuse it


# --- issue 4: concurrent batches still merge -------------------------------- #

def test_concurrent_batches_merge(tmp_path, monkeypatch):
    monkeypatch.setattr("shortforge.llm.openai_chat_raw", _fake_scored())
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    cfg.override("detect.hook_batch", 5)
    cfg.override("detect.hook_concurrency", 4)
    out = PH.score_transcript(_tr(20), cfg, _store())          # 4 batches in parallel
    assert len(out) == 20 and set(out) == set(range(20))       # all merged, none lost


# --- Blocker 1: half-size retry on timeout + partial-failure resilience ------ #

def _timeout_resp():
    return {"status": None, "body": "", "error": "The read operation timed out",
            "url": "u", "model": "m", "auth": "Authorization"}


def test_oversized_batch_times_out_then_splits_to_half(tmp_path, monkeypatch):
    """A batch that times out at N segments is retried at N/2, not re-sent whole."""
    monkeypatch.setattr("time.sleep", lambda s: None)          # no real backoff waits
    sizes_seen = []

    def fake(base, key, model, messages, **kw):
        import re
        idxs = [int(x) for x in re.findall(r"(?m)^(\d+)\t", messages[0]["content"])]
        sizes_seen.append(len(idxs))
        if len(idxs) >= 8:                                     # the full batch times out
            return _timeout_resp()
        scores = [{"index": i, "score": 0.6, "reason": "hook"} for i in idxs]
        return {"status": 200,
                "body": json.dumps({"choices": [{"message": {"content": json.dumps({"scores": scores})}}]}),
                "error": None, "url": base, "model": model, "auth": "Authorization"}

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    cfg.override("detect.hook_batch", 8)
    cfg.override("detect.hook_concurrency", 1)
    cfg.override("providers.retries", 0)                       # go straight to the split
    out = PH.score_transcript(_tr(8), cfg, _store())
    assert len(out) == 8 and set(out) == set(range(8))         # every segment scored via the halves
    assert 8 in sizes_seen and 4 in sizes_seen                 # tried whole, then split to 4


def test_dead_batch_does_not_fail_the_run(tmp_path, monkeypatch):
    """A chunk that keeps timing out (even at size 1) is skipped, not raised — the
    run continues with the segments that scored."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    def fake(base, key, model, messages, **kw):
        import re
        idxs = [int(x) for x in re.findall(r"(?m)^(\d+)\t", messages[0]["content"])]
        if any(i >= 6 for i in idxs):                         # second batch is permanently dead
            return _timeout_resp()
        scores = [{"index": i, "score": 0.6, "reason": "hook"} for i in idxs]
        return {"status": 200,
                "body": json.dumps({"choices": [{"message": {"content": json.dumps({"scores": scores})}}]}),
                "error": None, "url": base, "model": model, "auth": "Authorization"}

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    cfg.override("detect.hook_batch", 6)
    cfg.override("detect.hook_concurrency", 1)
    cfg.override("providers.retries", 0)
    out = PH.score_transcript(_tr(12), cfg, _store())         # never raises
    assert set(out) == set(range(6))                          # first batch scored
    assert all(i not in out for i in range(6, 12))            # dead batch's segments unscored


def test_partial_result_is_not_cached(tmp_path, monkeypatch):
    """A run with unscored segments must NOT write the cache, so a re-run retries."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    state = {"dead": True}

    def fake(base, key, model, messages, **kw):
        import re
        idxs = [int(x) for x in re.findall(r"(?m)^(\d+)\t", messages[0]["content"])]
        if state["dead"] and any(i >= 6 for i in idxs):
            return _timeout_resp()
        scores = [{"index": i, "score": 0.6, "reason": "hook"} for i in idxs]
        return {"status": 200,
                "body": json.dumps({"choices": [{"message": {"content": json.dumps({"scores": scores})}}]}),
                "error": None, "url": base, "model": model, "auth": "Authorization"}

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    cfg.override("detect.hook_batch", 6)
    cfg.override("detect.hook_concurrency", 1)
    cfg.override("providers.retries", 0)
    out1 = PH.score_transcript(_tr(12), cfg, _store())
    assert len(out1) == 6                                     # partial
    state["dead"] = False                                     # provider recovers
    out2 = PH.score_transcript(_tr(12), cfg, _store())        # would be a cache hit if cached
    assert set(out2) == set(range(12))                        # re-run retried the gap, all scored


# --- issue 1: clip-level selection, hook lands early ------------------------ #

def test_build_clips_from_hook_scores_is_clip_length():
    from shortforge.models import Candidate
    # 20 short segments; one strong hook at index 5
    segs = [Segment(i * 3.0, i * 3.0 + 3.0, f"sentence number {i} here.") for i in range(20)]
    tr = Transcript("en", 60.0, segs)
    cands = [Candidate(s.start, s.end, 0.9 if i == 5 else 0.1, "hook" if i == 5 else "")
             for i, s in enumerate(segs)]
    cfg = Config.load()
    cfg.override("select.num_clips", 1)
    cfg.override("select.target_duration", 30)
    cfg.override("select.tolerance", 8)
    clips = build_clips(tr, cands, cfg, "h")
    assert len(clips) == 1
    c = clips[0]
    assert (c.end - c.start) >= 20                             # a CLIP, not a 3s fragment
    assert c.start <= segs[5].start and segs[5].start - c.start <= 8   # hook is early, not buried


def test_max_backup_keeps_hook_near_start():
    from shortforge.models import Candidate
    # continuous speech (no pauses) so the grower would back up if unbounded
    segs = [Segment(i * 3.0, i * 3.0 + 3.0, f"and then thing {i} happened next.") for i in range(20)]
    tr = Transcript("en", 60.0, segs)
    cands = [Candidate(s.start, s.end, 0.9 if i == 10 else 0.1, "") for i, s in enumerate(segs)]
    cfg = Config.load()
    cfg.override("select.num_clips", 1)
    cfg.override("select.target_duration", 30)
    cfg.override("select.max_backup", 1)
    clips = build_clips(tr, cands, cfg, "h")
    anchor_start = segs[10].start
    assert anchor_start - clips[0].start <= 1 * 3.0 + 0.01     # backed up ≤ 1 segment


# --- issue 2: vision 400 surfaces the body ---------------------------------- #

def test_vision_400_reports_body(tmp_path, monkeypatch):
    from shortforge.models import Candidate
    st = {"providers": [], "credentials": [], "tasks": {}}
    c = S.add_credential(st, name="NVIDIA build", base_url="https://integrate.api.nvidia.com/v1",
                         api_key="nv")
    v = S.add_model(st, c["id"], model="meta/llama-3.2-11b-vision-instruct",
                    category="vision", tier="free")
    S.set_task_binding(st, "hook_detection", secondary=v["id"])
    S.save_store(st)

    # stub frame sampling (non-empty) + the vision call to return a 400 with a body
    monkeypatch.setattr("shortforge.providers.vision.sample_frames",
                        lambda *a, **k: ["f0.jpg", "f1.jpg"])
    monkeypatch.setattr("shortforge.providers.vision.score_frames",
                        lambda *a, **k: [{"status": 400, "body": '{"error":"image too large"}',
                                          "error": None, "url": "u", "model": "m", "images": 2}])
    cfg = Config.load()
    cands = [Candidate(0.0, 5.0, 0.9, "hook")]
    caught = {}
    import logging

    class _H(logging.Handler):
        def emit(self, rec):
            caught.setdefault("msgs", []).append(rec.getMessage())

    logging.getLogger("shortforge").addHandler(_H())
    out = PH.score_frames_for(cands, [0], "video.mp4", cfg, st)
    assert out == {}                                          # 400 → no score, logged
    joined = " ".join(caught.get("msgs", []))
    assert "image too large" in joined and "max_images" in joined   # full body surfaced
