"""Configurable timeouts + retry-with-backoff + hook-scoring chunking."""

import json
import re

import pytest

from shortforge.config import Config
from shortforge.models import Segment, Transcript
from shortforge import providers as P
from shortforge.providers import store as S
from shortforge.detect import provider_hooks as PH
from shortforge.utils import ShortForgeError


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "p.json"))
    monkeypatch.setattr("time.sleep", lambda s: None)      # no real backoff waits in tests


def _ok(content):
    return {"status": 200, "body": json.dumps({"choices": [{"message": {"content": content}}]}),
            "error": None, "url": "u", "model": "m", "auth": "Authorization"}


def _timeout():
    return {"status": None, "body": "", "error": "The read operation timed out",
            "url": "u", "model": "m", "auth": "Authorization"}


def test_task_timeout_defaults():
    assert S.task_timeout("hook_detection") == 600          # long batched call
    assert S.task_timeout("vision_scoring") == 300
    assert S.task_timeout("dub_translation") == 180
    assert S.task_timeout("does-not-exist") == 120          # global default


def test_retries_on_timeout_then_succeeds(monkeypatch):
    n = {"c": 0}

    def fake(*a, **k):
        n["c"] += 1
        return _timeout() if n["c"] == 1 else _ok("done")

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake)
    out = P.call_model_chat({"base_url": "b", "api_key": "k", "model": "m"},
                            [{"role": "user", "content": "x"}], timeout=5, retries=2)
    assert out == "done" and n["c"] == 2                     # retried once


def test_timeout_message_is_actionable(monkeypatch):
    monkeypatch.setattr("shortforge.llm.openai_chat_raw", lambda *a, **k: _timeout())
    with pytest.raises(ShortForgeError) as ei:
        P.call_model_chat({"base_url": "b", "api_key": "k", "model": "minimaxai/minimax-m3",
                           "name": "NVIDIA build · minimax"},
                          [{"role": "user", "content": "x"}], timeout=60, retries=2)
    msg = str(ei.value)
    assert "timed out after 60s" in msg and "3 attempt" in msg   # not a bare "read operation timed out"
    assert "timeout" in msg.lower()


def test_credential_timeout_overrides_argument(monkeypatch):
    seen = {}

    def fake(base, key, model, messages, *, timeout=120, **k):
        seen["timeout"] = timeout
        return _ok("ok")

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake)
    P.call_model_chat({"base_url": "b", "api_key": "k", "model": "m", "timeout": 450},
                      [{"role": "user", "content": "x"}], timeout=60)
    assert seen["timeout"] == 450                            # per-credential wins


def test_no_retry_on_a_401(monkeypatch):
    n = {"c": 0}

    def fake(*a, **k):
        n["c"] += 1
        return {"status": 401, "body": "nope", "error": None, "url": "u", "model": "m",
                "auth": "Authorization"}

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake)
    with pytest.raises(ShortForgeError):
        P.call_model_chat({"base_url": "b", "api_key": "k", "model": "m"},
                          [{"role": "user", "content": "x"}], retries=2)
    assert n["c"] == 1                                       # auth errors don't retry


def test_hook_scoring_chunks_and_merges(monkeypatch):
    store = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(store, name="NVIDIA build", base_url="https://x/v1", api_key="k")
    m = S.add_model(store, cred["id"], model="minimaxai/minimax-m3", category="llm")
    S.set_task_binding(store, "hook_detection", primary=m["id"])
    tr = Transcript("en", 90.0, [Segment(i * 3.0, i * 3.0 + 3.0, f"seg {i} text") for i in range(30)])
    calls = {"c": 0}

    def fake(base, key, model, messages, **k):
        calls["c"] += 1
        idxs = [int(x) for x in re.findall(r"(?m)^(\d+)\t", messages[0]["content"])]
        scores = [{"index": i, "score": 0.5, "reason": "r"} for i in idxs]
        return _ok(json.dumps({"scores": scores}))

    monkeypatch.setattr("shortforge.llm.openai_chat_raw", fake)
    cfg = Config.load()
    cfg.override("detect.hook_batch", 10)
    out = PH.score_transcript(tr, cfg, store)
    assert calls["c"] == 3                                   # 30 segments / batch 10
    assert len(out) == 30 and all(v[0] == 0.5 for v in out.values())   # merged globally
