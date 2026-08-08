"""Remote-control command vocabulary: bounded parsing, queue commands, and the
plain-English start/pause replies used by the ntfy permission gate."""

from shortforge import queue as Q
from shortforge import remote_control as RC


# --- parsing is bounded and whitelisted ------------------------------------- #

def test_parse_settings_whitelist_and_bounds():
    s = RC.parse_settings("clips=5 duration=120 res=720p aspect=portrait lang=en")
    assert s == {"num_clips": 5, "duration": 120, "resolution": "720p",
                 "aspect": "9:16", "language": "en"}
    # Unknown keys are ignored outright (no passthrough to config).
    assert RC.parse_settings("work_dir=/etc secret=1 output_dir=C:\\") == {}
    # Absurd or malformed values are dropped, not clamped into something odd.
    assert "num_clips" not in RC.parse_settings("clips=9999")
    assert "num_clips" not in RC.parse_settings("clips=0")
    assert "duration" not in RC.parse_settings("duration=99999")
    assert "duration" not in RC.parse_settings("duration=abc")


# --- the way the operator actually types ------------------------------------ #

def test_plain_english_from_the_phone():
    """'5 clips of 2 min from link 1' is how the operator writes it — nobody
    types duration=120 on a phone keyboard."""
    s = RC.parse_settings("https://youtu.be/AAA 5 clips of 2 min landscape")
    assert s == {"num_clips": 5, "duration": 120, "aspect": "16:9"}


def test_duration_units_and_mmss():
    assert RC.parse_settings("dur=2min")["duration"] == 120
    assert RC.parse_settings("duration=90s")["duration"] == 90
    assert RC.parse_settings("duration=1:30")["duration"] == 90
    assert RC.parse_settings("https://a/1 3 shorts 1:30 1080p") == {
        "num_clips": 3, "duration": 90, "resolution": "1080p"}


def test_aspect_is_read_before_duration():
    """'9:16' must be an aspect ratio, not nine minutes sixteen seconds."""
    s = RC.parse_settings("https://a/1 9:16 4 clips")
    assert s["aspect"] == "9:16" and "duration" not in s
    assert RC.parse_settings("https://a/1 16:9 2 clips")["aspect"] == "16:9"


def test_explicit_key_value_always_wins_over_loose_phrasing():
    s = RC.parse_settings("https://a/1 duration=45 make it 5 min long, 2 clips")
    assert s["duration"] == 45 and s["num_clips"] == 2


def test_numbers_are_never_read_out_of_a_link():
    """A URL full of digits must not become the clip count or duration."""
    assert RC.parse_settings("https://youtu.be/9x16clips8m") == {}
    assert RC.parse_settings("https://a/b?t=120s&n=9%20clips") == {}


def test_loose_values_are_bounded_like_explicit_ones():
    assert "num_clips" not in RC.parse_settings("https://a/1 900 clips")
    assert "duration" not in RC.parse_settings("https://a/1 5 hours")   # over 30 min


def test_the_reply_echoes_what_was_understood(tmp_path):
    """A misread '2 min' has to be visible before an hour of rendering."""
    reply = RC.handle_text("https://youtu.be/AAA 6 clips of 90s portrait", str(tmp_path))
    assert "6 clip(s) of 1m30s" in reply and "9:16" in reply


def test_parse_links_http_only_deduped_and_capped():
    assert RC.parse_links("no links here") == []
    assert RC.parse_links("file:///etc/passwd ftp://x/y") == []      # http(s) only
    assert RC.parse_links("https://a/1 https://a/1") == ["https://a/1"]   # deduped
    many = " ".join(f"https://a/{i}" for i in range(50))
    assert len(RC.parse_links(many)) == RC.MAX_LINKS_PER_MESSAGE      # capped


def test_links_get_queued_with_their_settings(tmp_path):
    work = str(tmp_path)
    reply = RC.handle_text("https://youtu.be/AAA https://youtu.be/BBB clips=5 duration=120",
                           work)
    assert "Queued" in reply and "2" in reply
    jobs = Q.load_queue(work)["jobs"]
    assert len(jobs) == 2
    assert all(j["settings"] == {"num_clips": 5, "duration": 120} for j in jobs)


def test_queue_size_is_capped(tmp_path, monkeypatch):
    work = str(tmp_path)
    monkeypatch.setattr(RC, "MAX_QUEUE", 3)
    RC.handle_text("https://a/1 https://a/2 https://a/3", work)
    reply = RC.handle_text("https://a/4", work)
    assert "full" in reply.lower()
    assert len(Q.load_queue(work)["jobs"]) == 3       # flood refused


# --- commands --------------------------------------------------------------- #

def test_commands(tmp_path):
    work = str(tmp_path)
    assert "ShortForge" in RC.handle_text("/help", work)
    assert "empty" in RC.handle_text("/list", work).lower()
    assert "Unknown command" in RC.handle_text("/rm -rf", work)      # no shell passthrough
    RC.handle_text("https://a/1", work)
    assert "1." in RC.handle_text("/list", work)
    assert "Dropped 1" in RC.handle_text("/cancel", work)
    assert Q.counts(Q.load_queue(work))[Q.PENDING] == 0


# --- remote on/off ------------------------------------------------------------ #

def test_pause_and_resume(tmp_path):
    work = str(tmp_path)
    RC.handle_text("https://a/1", work)
    assert "Paused" in RC.handle_text("/pause", work)
    assert Q.is_paused(Q.load_queue(work)) is True
    assert "PAUSED" in RC.handle_text("/status", work)
    assert "Working again" in RC.handle_text("/resume", work)
    assert Q.is_paused(Q.load_queue(work)) is False


def test_stop_and_on_aliases(tmp_path):
    work = str(tmp_path)
    RC.handle_text("/stop", work)
    assert Q.is_paused(Q.load_queue(work)) is True
    RC.handle_text("/on", work)
    assert Q.is_paused(Q.load_queue(work)) is False


def test_pause_does_not_lose_queued_links(tmp_path):
    work = str(tmp_path)
    RC.handle_text("/pause", work)
    RC.handle_text("https://a/1 clips=3", work)          # still accepted while paused
    q = Q.load_queue(work)
    assert Q.counts(q)[Q.PENDING] == 1 and Q.is_paused(q) is True


# --- plain-English replies to the "should I start?" permission prompt -------- #

def test_bare_start_words_resume_without_a_slash(tmp_path):
    work = str(tmp_path)
    RC.handle_text("/pause", work)
    for word in ("start", "go", "yes", "Begin", "OK"):
        RC.handle_text("/pause", work)
        assert Q.is_paused(Q.load_queue(work)) is True
        reply = RC.handle_text(word, work)
        assert "Working again" in reply
        assert Q.is_paused(Q.load_queue(work)) is False


def test_bare_pause_words_pause_without_a_slash(tmp_path):
    work = str(tmp_path)
    for word in ("pause", "stop", "no", "wait", "Not yet"):
        RC.handle_text("/resume", work)
        assert Q.is_paused(Q.load_queue(work)) is False
        reply = RC.handle_text(word, work)
        assert "Paused" in reply
        assert Q.is_paused(Q.load_queue(work)) is True


def test_start_word_does_not_hijack_a_real_link_message(tmp_path):
    """Only an exact bare reply counts — a link that happens to contain 'go'
    or 'start' in its text must still be queued normally, not swallowed."""
    work = str(tmp_path)
    reply = RC.handle_text("https://youtu.be/start-here go clips=2", work)
    assert "Queued" in reply
    assert len(Q.load_queue(work)["jobs"]) == 1
