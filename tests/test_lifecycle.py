"""Online/offline truthfulness.

The bug being locked out: the phone said "ShortForge is online and listening"
long after the program had been closed. A start-only notification is a memory,
not a status.
"""

import json
import os

import pytest

from shortforge import lifecycle as L
from shortforge import notify as N
from shortforge import queue as Q


@pytest.fixture(autouse=True)
def _reset():
    """The goodbye fires once per process; reset the guard between tests."""
    L._announced = False
    L._installed = False
    yield
    L._announced = False
    L._installed = False


def test_online_writes_state_and_offline_removes_it(tmp_path):
    work = str(tmp_path)
    assert L.mark_online(work, "queue") is None            # nothing before
    state = json.load(open(L.state_path(work), encoding="utf-8"))
    assert state["pid"] == os.getpid() and state["mode"] == "queue"

    sent = []
    assert L.announce_offline(work, "window closed", notify_fn=sent.append) is True
    assert not os.path.exists(L.state_path(work))           # no stale "still running"
    assert "stopped" in sent[0].lower() and "window closed" in sent[0]


def test_goodbye_is_sent_exactly_once(tmp_path):
    """Ctrl+C, the signal handler and atexit can all fire — the operator must
    still get one message, not three."""
    work = str(tmp_path)
    L.mark_online(work)
    sent = []
    assert L.announce_offline(work, "stopped", notify_fn=sent.append) is True
    assert L.announce_offline(work, "closed", notify_fn=sent.append) is False
    assert L.announce_offline(work, "closed", notify_fn=sent.append) is False
    assert len(sent) == 1


def test_goodbye_says_how_many_links_survive(tmp_path):
    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_job(q, "https://a/1")
    Q.add_job(q, "https://a/2")
    Q.save_queue(q, work)
    L.mark_online(work)
    sent = []
    L.announce_offline(work, "closed", notify_fn=sent.append)
    assert "2 link(s) still queued" in sent[0]
    assert "resume" in sent[0]


def test_a_leftover_state_file_is_reported_as_a_hard_stop(tmp_path):
    """Power cut: no goodbye was possible, so the NEXT start must say so."""
    work = str(tmp_path)
    L.mark_online(work, "listen")
    L.heartbeat(work, current={"url": "https://youtu.be/abc", "detail": "6 clip(s) of 1m00s"})
    L._announced = False

    prev = L.mark_online(work, "listen")              # restart without a clean exit
    note = L.interrupted_note(prev)
    assert "without shutting down" in note
    assert "https://youtu.be/abc" in note and "6 clip(s)" in note
    assert "Nothing was lost" in note


def test_clean_shutdown_produces_no_scary_note(tmp_path):
    work = str(tmp_path)
    L.mark_online(work)
    L.announce_offline(work, "closed", notify_fn=lambda m: None)
    L._announced = False
    assert L.interrupted_note(L.mark_online(work)) == ""     # state file was removed


def test_heartbeat_records_the_link_in_flight(tmp_path):
    work = str(tmp_path)
    L.mark_online(work)
    L.heartbeat(work, current={"url": "https://a/1", "detail": "3 clip(s)"})
    assert L.read_state(work)["current"]["url"] == "https://a/1"
    L.heartbeat(work, current=None)                          # explicit "between jobs"
    assert L.read_state(work)["current"] is None


def test_install_exit_notice_is_idempotent(tmp_path):
    L.install_exit_notice(str(tmp_path))
    L.install_exit_notice(str(tmp_path))                     # must not double-register
    assert L._installed is True


# --- network outages -------------------------------------------------------- #

def test_outage_is_announced_after_repeated_failures_not_one_blip():
    sent = []
    w = N.OutageWatch("ntfy", threshold=3, notify_fn=sent.append)
    assert w.record(False) is None and w.record(False) is None   # blips stay quiet
    assert sent == []
    assert w.record(False) is not None                           # third strike
    assert w.down is True
    assert "Network problem" in sent[0] and "ntfy" in sent[0]
    assert len(sent) == 1
    w.record(False)                                              # no repeat spam
    assert len(sent) == 1


def test_recovery_is_reported_because_the_outage_alert_may_never_arrive():
    sent = []
    w = N.OutageWatch("ntfy", threshold=1, notify_fn=sent.append)
    w.record(False)
    w.record(True)
    assert w.down is False
    assert "Back online" in sent[-1] and "Nothing was lost" in sent[-1]


def test_a_healthy_connection_says_nothing():
    sent = []
    w = N.OutageWatch(notify_fn=sent.append)
    for _ in range(10):
        assert w.record(True) is None
    assert sent == []
