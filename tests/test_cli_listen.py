"""`cli.py listen` — the duplicate-instance guard.

Two ntfy pollers on the same command topic would each act on the same
incoming message (double-queuing every phone-sent link), and nothing dedupes
against that once handle_text() has run — so a second `listen` must refuse to
start on top of a live one, but never refuse because of a stale/dead PID's
leftover state (that would block a legitimate restart after a crash)."""

import time

import cli
from shortforge import utils


def test_no_existing_state_means_nothing_is_running():
    assert cli._listener_already_running(None, own_pid=123) is False


def test_own_pid_is_never_treated_as_a_duplicate():
    """A process reading its own freshly-written state (the normal case
    right after mark_online) must not refuse to start against itself."""
    state = {"pid": 555, "last_seen": time.time()}
    assert cli._listener_already_running(state, own_pid=555) is False


def test_a_live_recent_other_pid_is_refused(monkeypatch):
    monkeypatch.setattr(utils, "pid_alive", lambda pid: True)
    state = {"pid": 999, "last_seen": time.time()}
    assert cli._listener_already_running(state, own_pid=123) is True


def test_a_dead_pid_never_blocks_even_if_last_seen_looks_fresh(monkeypatch):
    """Windows can reuse PIDs; a fresh timestamp alone must not be trusted."""
    monkeypatch.setattr(utils, "pid_alive", lambda pid: False)
    state = {"pid": 999, "last_seen": time.time()}
    assert cli._listener_already_running(state, own_pid=123) is False


def test_a_stale_last_seen_never_blocks_even_if_the_pid_looks_alive(monkeypatch):
    """A live PID with an old checked-in time (paused process, clock skew,
    PID reuse racing the check) must not block a restart either."""
    monkeypatch.setattr(utils, "pid_alive", lambda pid: True)
    state = {"pid": 999, "last_seen": time.time() - 60}
    assert cli._listener_already_running(state, own_pid=123, freshness_s=15) is False
