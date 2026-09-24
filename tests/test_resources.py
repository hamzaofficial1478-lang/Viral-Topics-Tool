"""The resource footer: honest numbers, and never able to break the dashboard.

The operator's recurring question is "is it stuck, or just slow?" — which no
message can settle, because it is really "is anything still working". A live
reading of what ShortForge itself is using answers it: CPU moving means work
is happening.
"""

from shortforge import resources as R


def test_snapshot_reports_the_machine():
    s = R.snapshot()
    assert s["cores"] >= 1
    assert "has_psutil" in s
    if s["has_psutil"]:
        assert s["mem_total"] > 0
        assert 0 <= s["mem_pct"] <= 100
        assert s["proc_mem"] > 0            # this very process
        assert s["proc_count"] >= 1


def test_snapshot_never_raises_even_without_psutil(monkeypatch):
    """Degrades to fewer numbers rather than taking the page down with it."""
    monkeypatch.setattr(R, "_psutil", lambda: None)
    s = R.snapshot()
    assert s["has_psutil"] is False
    assert s["cores"] >= 1                  # stdlib still answers this


def test_snapshot_survives_a_broken_psutil(monkeypatch):
    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError("psutil is unhappy")

    monkeypatch.setattr(R, "_psutil", lambda: Exploding())
    monkeypatch.setattr(R, "_proc", None)
    assert R.snapshot()["cores"] >= 1       # no exception escapes


def test_gpu_name_always_answers_something():
    assert isinstance(R.gpu_name(), str) and R.gpu_name()


def test_render_speed_is_none_when_nothing_is_running(tmp_path):
    assert R.render_speed(str(tmp_path)) is None


def test_render_speed_reports_elapsed_for_a_live_job(tmp_path):
    import time
    from shortforge import lifecycle
    work = str(tmp_path)
    lifecycle.mark_online(work, "listen")
    lifecycle.heartbeat(work, current={"url": "https://a/1",
                                       "started": time.time() - 300})
    assert "5 min" in R.render_speed(work)


def test_human_bytes_scales():
    assert R.human_bytes(5_000) == "5 KB"
    assert R.human_bytes(5 * 1024 ** 2) == "5 MB"
    assert R.human_bytes(3 * 1024 ** 3) == "3.0 GB"
    assert R.human_bytes(None) == "?"


def test_the_error_agent_has_its_own_routing_slot():
    """So the operator can bind their strongest model to failure analysis
    specifically, instead of it borrowing whatever metadata uses."""
    from shortforge.providers.store import TASKS, _TASK_BY_KEY
    t = _TASK_BY_KEY.get("error_agent")
    assert t is not None, "error_agent is missing from the routing table"
    assert "llm" in t["cats"] and t["on"] is True
    assert any(x["key"] == "error_agent" for x in TASKS)
