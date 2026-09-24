"""`cli.py queue run`'s end-of-run notification must match what actually ran.

The operator pressed "▶ Start working" and their phone received, all at once:
    1. ▶️ ShortForge started
    2. 🏁 All links processed
    3. 🔴 ShortForge stopped
…while the link they had queued was still pending and had never begun.

Message 2 was the lie: the summary was a single unconditional string sent
regardless of what `drain_queue` had done, and a drain that exited instantly
(queue paused / another worker holding the lock) returned a dict that looked
exactly like a completed run. CLAUDE.md rule 1 — never emit output that looks
successful but isn't.
"""

import cli


def _s(reason, **kw):
    base = {"made": 0, "done": 0, "failed": 0, "pending": 0, "total_clips": 0,
            "elapsed": 0.0, "stopped_because": reason}
    base.update(kw)
    return base


def test_a_paused_run_never_claims_links_were_processed():
    msg = cli._queue_summary_message(_s("paused", pending=1), "/out")
    assert "All links processed" not in msg
    assert "paused" in msg.lower() and "1 link(s) still pending" in msg
    assert "start" in msg.lower()          # tells them how to actually begin


def test_a_locked_run_says_another_worker_owns_the_queue():
    msg = cli._queue_summary_message(_s("locked", pending=3), "/out")
    assert "All links processed" not in msg
    assert "already working" in msg.lower() and "3 link(s) still pending" in msg


def test_an_interrupted_run_reports_partial_work():
    msg = cli._queue_summary_message(_s("asked_to_stop", made=2, pending=4), "/out")
    assert "All links processed" not in msg
    assert "2 clip(s)" in msg and "4 link(s) still pending" in msg


def test_a_real_completion_still_reports_success():
    msg = cli._queue_summary_message(
        _s("empty", made=3, done=2, total_clips=3, elapsed=125.0), "/out/dir")
    assert "All links processed" in msg
    assert "2 done" in msg and "3 clip(s)" in msg and "/out/dir" in msg


def test_a_missing_reason_defaults_to_the_completion_message():
    """Older callers / any dict without the key must not crash."""
    msg = cli._queue_summary_message({"done": 1, "failed": 0, "total_clips": 1}, "/out")
    assert "All links processed" in msg
