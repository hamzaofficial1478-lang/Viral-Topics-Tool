"""Telegram control surface: owner-only gate, bounded parsing, queue commands."""

import json

import pytest

from shortforge import queue as Q
from shortforge import telegram_bot as TB


# --- SECURITY: only the owner's chat may command the machine ---------------- #

def test_non_owner_updates_are_dropped(tmp_path, monkeypatch):
    """A stranger who finds the bot must not be able to queue anything, and must
    not even get a reply."""
    work = str(tmp_path)
    replies, handled = [], []
    monkeypatch.setattr(TB, "_call", lambda *a, **k: {"result": [
        {"update_id": 1, "message": {"chat": {"id": 999999}, "text": "https://evil/x clips=50"}},
    ]})
    monkeypatch.setattr(TB, "_reply", lambda t, c, m: replies.append(m))
    monkeypatch.setattr(TB, "handle_text", lambda *a, **k: handled.append(a) or "x")

    off = TB.poll_once("tok", owner_chat="123", offset=0, work_dir=work)
    assert off == 2                                  # offset still advances (no reprocessing)
    assert replies == [] and handled == []           # nothing parsed, nothing answered
    assert Q.load_queue(work)["jobs"] == []          # nothing queued


def test_owner_update_is_handled(tmp_path, monkeypatch):
    work = str(tmp_path)
    replies = []
    monkeypatch.setattr(TB, "_call", lambda *a, **k: {"result": [
        {"update_id": 5, "message": {"chat": {"id": 123}, "text": "/status"}},
    ]})
    monkeypatch.setattr(TB, "_reply", lambda t, c, m: replies.append(m))
    off = TB.poll_once("tok", owner_chat="123", offset=0, work_dir=work)
    assert off == 6 and len(replies) == 1 and "Queue" in replies[0]


def test_handler_errors_do_not_kill_the_listener(tmp_path, monkeypatch):
    work = str(tmp_path)
    replies = []
    monkeypatch.setattr(TB, "_call", lambda *a, **k: {"result": [
        {"update_id": 1, "message": {"chat": {"id": 123}, "text": "hi"}}]})
    monkeypatch.setattr(TB, "_reply", lambda t, c, m: replies.append(m))
    monkeypatch.setattr(TB, "handle_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    TB.poll_once("tok", "123", 0, work)               # must not raise
    assert "went wrong" in replies[0]


def test_network_failure_returns_same_offset(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(TB, "_call",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))
    assert TB.poll_once("tok", "123", 7, str(tmp_path)) == 7     # nothing lost


# --- parsing is bounded and whitelisted ------------------------------------- #

def test_parse_settings_whitelist_and_bounds():
    s = TB.parse_settings("clips=5 duration=120 res=720p aspect=portrait lang=en")
    assert s == {"num_clips": 5, "duration": 120, "resolution": "720p",
                 "aspect": "9:16", "language": "en"}
    # Unknown keys are ignored outright (no passthrough to config).
    assert TB.parse_settings("work_dir=/etc secret=1 output_dir=C:\\") == {}
    # Absurd or malformed values are dropped, not clamped into something odd.
    assert "num_clips" not in TB.parse_settings("clips=9999")
    assert "num_clips" not in TB.parse_settings("clips=0")
    assert "duration" not in TB.parse_settings("duration=99999")
    assert "duration" not in TB.parse_settings("duration=abc")


def test_parse_links_http_only_deduped_and_capped():
    assert TB.parse_links("no links here") == []
    assert TB.parse_links("file:///etc/passwd ftp://x/y") == []      # http(s) only
    assert TB.parse_links("https://a/1 https://a/1") == ["https://a/1"]   # deduped
    many = " ".join(f"https://a/{i}" for i in range(50))
    assert len(TB.parse_links(many)) == TB.MAX_LINKS_PER_MESSAGE      # capped


def test_links_get_queued_with_their_settings(tmp_path):
    work = str(tmp_path)
    reply = TB.handle_text("https://youtu.be/AAA https://youtu.be/BBB clips=5 duration=120",
                           work)
    assert "Queued" in reply and "2" in reply
    jobs = Q.load_queue(work)["jobs"]
    assert len(jobs) == 2
    assert all(j["settings"] == {"num_clips": 5, "duration": 120} for j in jobs)


def test_queue_size_is_capped(tmp_path, monkeypatch):
    work = str(tmp_path)
    monkeypatch.setattr(TB, "MAX_QUEUE", 3)
    TB.handle_text("https://a/1 https://a/2 https://a/3", work)
    reply = TB.handle_text("https://a/4", work)
    assert "full" in reply.lower()
    assert len(Q.load_queue(work)["jobs"]) == 3       # flood refused


# --- commands --------------------------------------------------------------- #

def test_commands(tmp_path):
    work = str(tmp_path)
    assert "ShortForge" in TB.handle_text("/help", work)
    assert "empty" in TB.handle_text("/list", work).lower()
    assert "Unknown command" in TB.handle_text("/rm -rf", work)      # no shell passthrough
    TB.handle_text("https://a/1", work)
    assert "1." in TB.handle_text("/list", work)
    assert "Dropped 1" in TB.handle_text("/cancel", work)
    assert Q.counts(Q.load_queue(work))[Q.PENDING] == 0
