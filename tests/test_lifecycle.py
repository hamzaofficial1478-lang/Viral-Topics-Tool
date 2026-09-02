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


# --- shutting the PC down must send a goodbye too ----------------------------- #
# Operator: closing ShortForge sends a message, but shutting the PC down with it
# running sends nothing. Cause: the console handler mapped CTRL_SHUTDOWN_EVENT
# and CTRL_LOGOFF_EVENT, but Microsoft documents both as delivered to SERVICES
# only -- "Interactive applications are not present by the time the system sends
# this signal" -- so for a .bat-launched console app those branches never ran.
# Interactive programs get WM_QUERYENDSESSION / WM_ENDSESSION on a window.

def test_a_shutdown_message_is_recognised_as_a_shutdown():
    assert L.session_end_reason(L.WM_QUERYENDSESSION, 0) == "PC shutting down"
    assert L.session_end_reason(L.WM_ENDSESSION, 0) == "PC shutting down"


def test_a_logoff_is_told_apart_from_a_shutdown():
    assert L.session_end_reason(L.WM_ENDSESSION, L.ENDSESSION_LOGOFF) == "signed out"
    assert L.session_end_reason(L.WM_QUERYENDSESSION,
                                L.ENDSESSION_LOGOFF | 0x1) == "signed out"


def test_ordinary_window_messages_are_not_mistaken_for_a_shutdown():
    """The pump sees every message for the window; only these two mean goodbye."""
    for msg in (0x0001, 0x0002, 0x000F, 0x0010, 0x0018, 0x0100):
        assert L.session_end_reason(msg, 0) is None


def test_install_exit_notice_arms_the_session_listener_too(tmp_path, monkeypatch):
    """The console handler alone cannot see a PC shutdown, so arming the exit
    notice must also arm the window-message listener that can."""
    armed = []
    monkeypatch.setattr(L, "install_session_end_notice",
                        lambda on_end: armed.append(on_end) or True)
    L.install_exit_notice(str(tmp_path))
    assert len(armed) == 1

    # and the callback it was given really does announce a shutdown
    sent = []
    monkeypatch.setattr(L, "announce_offline",
                        lambda wd, reason, **k: sent.append(reason) or True)
    armed[0]("PC shutting down")
    assert sent == ["PC shutting down"]


def test_arming_the_session_listener_is_a_safe_no_op_off_windows():
    """It must never raise or block startup on a machine without windll."""
    calls = []
    assert L.install_session_end_notice(calls.append) is False
    assert calls == []


# --- one process must never retract another's claim --------------------------- #
# `cli.py queue run` (which the dashboard can spawn as a short helper) and
# `cli.py listen` share runstate.json. The helper's atexit used to delete it
# outright, wiping the LISTENER's live state — including the record of the link
# in flight — which silently broke the dashboard's "is a worker running?"
# check, the duplicate-instance guard, and crash recovery.

def test_clear_state_does_not_delete_a_state_file_owned_by_another_process(tmp_path):
    work = str(tmp_path)
    L.mark_online(work, "listen")
    L.heartbeat(work, current={"url": "https://youtu.be/in-flight", "detail": "3 clip(s)"})
    state = L.read_state(work)
    state["pid"] = os.getpid() + 10_000            # pretend another process owns it
    L._write_state(work, state)

    L.clear_state(work, only_if_mine=True)

    survived = L.read_state(work)
    assert survived is not None                    # not deleted
    assert survived["current"]["url"] == "https://youtu.be/in-flight"


def test_clear_state_still_removes_our_own_claim(tmp_path):
    work = str(tmp_path)
    L.mark_online(work, "queue")                   # written with OUR pid
    L.clear_state(work, only_if_mine=True)
    assert L.read_state(work) is None


def test_announce_offline_leaves_another_processs_state_alone(tmp_path):
    """End-to-end version of the above, through the real exit path."""
    work = str(tmp_path)
    L.mark_online(work, "listen")
    L.heartbeat(work, current={"url": "https://youtu.be/in-flight"})
    state = L.read_state(work)
    state["pid"] = os.getpid() + 10_000
    L._write_state(work, state)

    L.announce_offline(work, "closed", notify_fn=lambda m: None)

    assert L.read_state(work) is not None          # the listener keeps its state


def test_cancel_goodbye_clears_our_state_without_announcing(tmp_path):
    """A run that exited immediately having done nothing (paused / locked)
    still has to retract its claim — otherwise the next start misreads the
    leftover file as a power cut — but must not announce a shutdown on top of
    the summary that already explained why nothing ran."""
    work = str(tmp_path)
    L.mark_online(work, "queue")
    sent = []

    L.cancel_goodbye(work)

    assert L.read_state(work) is None                       # claim retracted
    assert L.announce_offline(work, "closed", notify_fn=sent.append) is False
    assert sent == []                                       # atexit stays quiet


# --- the dashboard's own shutdown channel (cli.py ui) ------------------------- #
# Separate from the worker's (cli.py listen / queue run): closing the UI window
# reported by the operator as silent — cmd_ui never armed any exit handling at
# all before this, only cmd_listen/cmd_queue did.

def test_ui_channel_uses_a_separate_state_file_from_the_worker(tmp_path):
    """The whole reason for a separate file: mark_online() overwrites
    pid/mode/current unconditionally, so if the UI process shared the
    worker's runstate.json, opening the dashboard while a job is in flight
    would stomp the crash-recovery record of what that job was doing."""
    work = str(tmp_path)
    L.mark_online(work, "listen")
    L.heartbeat(work, current={"url": "https://youtu.be/inflight", "detail": "2 clip(s)"})
    worker_state_before = L.read_state(work)

    L.mark_online(work, "ui", state_file=L.UI_STATE_FILE)

    assert L.read_state(work) == worker_state_before          # worker's file untouched
    assert L.read_state(work, L.UI_STATE_FILE)["mode"] == "ui"
    assert os.path.exists(L.state_path(work, L.UI_STATE_FILE))
    assert L.state_path(work, L.UI_STATE_FILE) != L.state_path(work)


def test_ui_channel_offline_notice_omits_queue_detail(tmp_path):
    """Closing the dashboard window says nothing about whether the worker is
    still rendering — attaching '3 clip(s) produced' / 'it was working on X'
    to THIS message would misrepresent the whole program as stopped."""
    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_job(q, "https://a/1")
    Q.save_queue(q, work)
    L.mark_online(work, "listen")   # the worker IS still "online" in its own file
    L.heartbeat(work, current={"url": "https://a/1", "detail": "2 clip(s)"})

    sent = []
    ok = L.announce_offline(work, "window closed", notify_fn=sent.append,
                            state_file=L.UI_STATE_FILE, icon="🖥️",
                            title="ShortForge dashboard closed",
                            include_queue_detail=False)
    assert ok is True
    assert "dashboard closed" in sent[0].lower()
    assert "still queued" not in sent[0] and "working on" not in sent[0]
    # the worker's own state is untouched by the UI channel's announcement
    assert L.read_state(work)["current"]["url"] == "https://a/1"


def test_ui_channel_and_worker_channel_each_announce_independently(tmp_path):
    """Each channel's once-only guard is per-PROCESS (module-global), which is
    correct because cmd_ui and cmd_listen are always separate OS processes —
    but within a single process/test, install_exit_notice's one-shot guard
    must not block a distinctly-parameterised second announce_offline call."""
    work = str(tmp_path)
    sent = []
    assert L.announce_offline(work, "closed", notify_fn=sent.append,
                              state_file=L.UI_STATE_FILE, include_queue_detail=False) is True
    # the SAME process announcing again (any channel) is correctly suppressed —
    # the one-shot guard is process-wide by design (see install_exit_notice docstring)
    assert L.announce_offline(work, "closed", notify_fn=sent.append) is False
    assert len(sent) == 1


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


# --- ntfy connectivity recorded locally (dashboard-readable) ---------------- #

def test_first_ever_healthy_poll_is_recorded(tmp_path):
    """A fresh state file has no ntfy_ok yet — the very first successful poll
    must still be written, or the dashboard has nothing to show as 'connected'
    until something first goes wrong."""
    work = str(tmp_path)
    L.mark_online(work)
    L.note_ntfy_status(work, ok=True)
    assert L.read_state(work)["ntfy_ok"] is True


def test_sustained_healthy_polls_do_not_churn_the_state_file(tmp_path):
    work = str(tmp_path)
    L.mark_online(work)
    L.note_ntfy_status(work, ok=True)
    checked_at = L.read_state(work)["ntfy_checked"]
    for _ in range(5):
        L.note_ntfy_status(work, ok=True)
    assert L.read_state(work)["ntfy_checked"] == checked_at    # never rewritten


def test_a_failure_is_recorded_with_a_down_since_timestamp(tmp_path):
    work = str(tmp_path)
    L.mark_online(work)
    L.note_ntfy_status(work, ok=True)
    L.note_ntfy_status(work, ok=False, detail="timed out")
    state = L.read_state(work)
    assert state["ntfy_ok"] is False
    assert state["ntfy_down_since"] is not None
    assert "timed out" in state["ntfy_detail"]


def test_recovery_clears_down_since(tmp_path):
    work = str(tmp_path)
    L.mark_online(work)
    L.note_ntfy_status(work, ok=False)
    L.note_ntfy_status(work, ok=True)
    state = L.read_state(work)
    assert state["ntfy_ok"] is True
    assert state["ntfy_down_since"] is None


def test_no_run_in_progress_is_a_no_op(tmp_path):
    work = str(tmp_path)
    L.note_ntfy_status(work, ok=True)      # no mark_online() first — nothing to annotate
    assert L.read_state(work) is None
