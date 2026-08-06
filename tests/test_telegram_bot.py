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


# --- the way the operator actually types ------------------------------------ #

def test_plain_english_from_the_phone():
    """'5 clips of 2 min from link 1' is how the operator writes it — nobody
    types duration=120 on a phone keyboard."""
    s = TB.parse_settings("https://youtu.be/AAA 5 clips of 2 min landscape")
    assert s == {"num_clips": 5, "duration": 120, "aspect": "16:9"}


def test_duration_units_and_mmss():
    assert TB.parse_settings("dur=2min")["duration"] == 120
    assert TB.parse_settings("duration=90s")["duration"] == 90
    assert TB.parse_settings("duration=1:30")["duration"] == 90
    assert TB.parse_settings("https://a/1 3 shorts 1:30 1080p") == {
        "num_clips": 3, "duration": 90, "resolution": "1080p"}


def test_aspect_is_read_before_duration():
    """'9:16' must be an aspect ratio, not nine minutes sixteen seconds."""
    s = TB.parse_settings("https://a/1 9:16 4 clips")
    assert s["aspect"] == "9:16" and "duration" not in s
    assert TB.parse_settings("https://a/1 16:9 2 clips")["aspect"] == "16:9"


def test_explicit_key_value_always_wins_over_loose_phrasing():
    s = TB.parse_settings("https://a/1 duration=45 make it 5 min long, 2 clips")
    assert s["duration"] == 45 and s["num_clips"] == 2


def test_numbers_are_never_read_out_of_a_link():
    """A URL full of digits must not become the clip count or duration."""
    assert TB.parse_settings("https://youtu.be/9x16clips8m") == {}
    assert TB.parse_settings("https://a/b?t=120s&n=9%20clips") == {}


def test_loose_values_are_bounded_like_explicit_ones():
    assert "num_clips" not in TB.parse_settings("https://a/1 900 clips")
    assert "duration" not in TB.parse_settings("https://a/1 5 hours")   # over 30 min


def test_the_reply_echoes_what_was_understood(tmp_path):
    """A misread '2 min' has to be visible before an hour of rendering."""
    reply = TB.handle_text("https://youtu.be/AAA 6 clips of 90s portrait", str(tmp_path))
    assert "6 clip(s) of 1m30s" in reply and "9:16" in reply


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


# --- remote on/off ---------------------------------------------------------- #

def test_pause_and_resume_from_telegram(tmp_path):
    work = str(tmp_path)
    TB.handle_text("https://a/1", work)
    assert "Paused" in TB.handle_text("/pause", work)
    assert Q.is_paused(Q.load_queue(work)) is True
    assert "PAUSED" in TB.handle_text("/status", work)
    assert "Working again" in TB.handle_text("/resume", work)
    assert Q.is_paused(Q.load_queue(work)) is False


def test_stop_and_on_aliases(tmp_path):
    work = str(tmp_path)
    TB.handle_text("/stop", work)
    assert Q.is_paused(Q.load_queue(work)) is True
    TB.handle_text("/on", work)
    assert Q.is_paused(Q.load_queue(work)) is False


def test_pause_does_not_lose_queued_links(tmp_path):
    work = str(tmp_path)
    TB.handle_text("/pause", work)
    TB.handle_text("https://a/1 clips=3", work)          # still accepted while paused
    q = Q.load_queue(work)
    assert Q.counts(q)[Q.PENDING] == 1 and Q.is_paused(q) is True
