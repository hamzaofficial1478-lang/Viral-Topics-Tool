"""Remote-control command vocabulary: bounded parsing, queue commands, and the
plain-English start/pause replies used by the ntfy permission gate."""

import pytest

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


# --- natural phrasing for start/pause ----------------------------------------- #
# The operator sent a link from their phone, then tried to start it from their
# phone, and nothing happened. Cause: only an EXACT bare "start"/"go"/"yes"
# counted — even plain "resume" (the twin of /resume) was unrecognised, and
# every natural phrasing fell through to a flat "Send me a video link", which
# neither started anything nor hinted that the queue was sitting paused.

@pytest.mark.parametrize("phrase", [
    "resume", "start working", "start the process", "start it", "start now",
    "go ahead", "yes please", "continue", "proceed", "begin the task",
    "start.", "GO!", "  Start Working  ",
])
def test_natural_start_phrasings_all_resume(tmp_path, phrase):
    work = str(tmp_path)
    RC.handle_text("/pause", work)
    assert Q.is_paused(Q.load_queue(work)) is True
    RC.handle_text(phrase, work)
    assert Q.is_paused(Q.load_queue(work)) is False, f"{phrase!r} did not resume"


@pytest.mark.parametrize("phrase", ["hold on", "stop it", "not now", "wait a bit", "pause."])
def test_natural_pause_phrasings_all_pause(tmp_path, phrase):
    work = str(tmp_path)
    RC.handle_text("/resume", work)
    RC.handle_text(phrase, work)
    assert Q.is_paused(Q.load_queue(work)) is True, f"{phrase!r} did not pause"


@pytest.mark.parametrize("text", [
    "https://youtu.be/start-working-guide 2 clips",
    "https://youtu.be/go-ahead-and-watch",
    "https://youtu.be/how-to-cancel-a-plan clips=3",
])
def test_loosened_matching_never_swallows_a_link_message(tmp_path, text):
    """The safety property that lets the vocabulary above be generous: a
    message containing a URL is never tested against the command words."""
    work = str(tmp_path)
    reply = RC.handle_text(text, work)
    assert "Queued" in reply
    assert len(Q.load_queue(work)["jobs"]) == 1


def test_an_unrecognised_message_reports_the_queue_state_instead_of_a_dead_end(tmp_path):
    """"Send me a video link" for "start working" gave the operator no way to
    discover the word that would have worked. Any unrecognised message now
    reports what the queue is actually doing and which words act on it."""
    work = str(tmp_path)
    RC.handle_text("https://a/1", work)
    RC.handle_text("/pause", work)

    reply = RC.handle_text("please make the videos now", work)

    assert "didn't understand" in reply
    assert "PAUSED" in reply and "1 pending" in reply
    assert "start" in reply.lower()          # the word that would have worked


def test_queueing_into_a_paused_queue_says_it_will_not_start_yet(tmp_path):
    """The confirmation used to promise "I'll message you when each one starts
    and finishes" even when the queue was paused and nothing was going to
    start — which is why "I added the link and nothing happened" kept coming
    back. The reply has to distinguish the two cases."""
    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.set_paused(q, True)
    Q.save_queue(q, work)

    reply = RC.handle_text("https://youtu.be/AAAAAAAAAAA", work)

    assert "paused" in reply.lower()
    assert "start" in reply.lower()                     # names the way forward
    assert "I'll message you when each one starts" not in reply   # no false promise


def test_queueing_into_a_running_queue_says_it_is_getting_on_with_it(tmp_path):
    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.set_paused(q, False)
    Q.save_queue(q, work)

    reply = RC.handle_text("https://youtu.be/AAAAAAAAAAA", work)

    assert "paused" not in reply.lower()
    assert "starts and finishes" in reply


@pytest.mark.parametrize("cmd", ["/start", "/resume", "/on", "/go", "/run"])
def test_slash_start_actually_starts(tmp_path, cmd):
    """`/start` returned the HELP text and left the queue paused — a leftover of
    the Telegram convention where /start is a bot's intro. The operator replied
    "/start" to the "shall I begin?" prompt, got a wall of help, and nothing
    ran. A command called start that does not start is a trap."""
    work = str(tmp_path)
    RC.handle_text("https://a/1", work)
    RC.handle_text("/pause", work)
    assert Q.is_paused(Q.load_queue(work)) is True

    reply = RC.handle_text(cmd, work)

    assert Q.is_paused(Q.load_queue(work)) is False, f"{cmd} did not start the queue"
    assert "Working again" in reply


def test_help_is_still_reachable_on_its_own_command(tmp_path):
    work = str(tmp_path)
    assert "send me links" in RC.handle_text("/help", work)


def test_help_documents_that_the_slash_is_optional(tmp_path):
    """The operator asked outright which form works. Both do — and the help
    now says so instead of listing only the slash spellings."""
    text = RC.handle_text("/help", str(tmp_path))
    assert "slash is optional" in text
    assert "start" in text and "cancel" in text


def test_status_flags_a_queue_that_is_unpaused_with_work_but_nothing_running(tmp_path):
    """The silent-stall shape. With the worker lock wedged, "start" looked like
    it worked and nothing happened, with no way to tell from the phone."""
    work = str(tmp_path)
    RC.handle_text("https://a/1", work)
    RC.handle_text("start", work)

    status = RC.handle_text("/status", work)

    assert "Nothing is running" in status
    assert "lock" in status.lower()


def test_status_says_nothing_alarming_when_the_queue_is_simply_paused(tmp_path):
    work = str(tmp_path)
    RC.handle_text("https://a/1", work)
    RC.handle_text("pause", work)
    assert "Nothing is running even though" not in RC.handle_text("/status", work)


# --- plain-English /cancel and /clear ----------------------------------------- #
# The exact operator-reported bug: cancelled a job by texting "cancel" (no
# slash, having learned "pause"/"start" work that way), then found it still
# running in the Queue screen — because a bare "cancel" fell through to the
# generic "Send me a video link" reply and never reached /cancel at all.

def test_bare_cancel_word_drops_pending_jobs_without_a_slash(tmp_path):
    work = str(tmp_path)
    RC.handle_text("https://a/1", work)
    RC.handle_text("https://a/2", work)
    reply = RC.handle_text("cancel", work)
    assert "Dropped 2" in reply
    assert Q.counts(Q.load_queue(work))[Q.PENDING] == 0


def test_bare_clear_word_removes_finished_jobs_without_a_slash(tmp_path):
    work = str(tmp_path)
    q = Q.load_queue(work)
    Q.add_job(q, "https://a/1")
    Q.mark(q, q["jobs"][0]["id"], Q.DONE, clips=["a.mp4"])
    Q.save_queue(q, work)
    reply = RC.handle_text("clear", work)
    assert "Removed 1" in reply
    assert Q.load_queue(work)["jobs"] == []


def test_cancel_word_does_not_hijack_a_real_link_message(tmp_path):
    """Same guard as the start/pause words: a link message that happens to
    contain the word 'cancel' must still be queued, not swallowed."""
    work = str(tmp_path)
    reply = RC.handle_text("https://youtu.be/cancel-policy-video clips=2", work)
    assert "Queued" in reply
    assert len(Q.load_queue(work)["jobs"]) == 1


# --- shared chat log: ntfy and the dashboard Chat screen show one conversation - #

def test_log_exchange_round_trips(tmp_path):
    work = str(tmp_path)
    RC.log_exchange(work, "ntfy", "hello from phone", "hi back")
    RC.log_exchange(work, "dashboard", "hello from browser", "hi there too")
    log = RC.read_chat_log(work)
    assert len(log) == 2
    assert log[0]["source"] == "ntfy" and log[0]["text"] == "hello from phone"
    assert log[1]["source"] == "dashboard" and log[1]["reply"] == "hi there too"


def test_read_chat_log_missing_file_is_empty(tmp_path):
    assert RC.read_chat_log(str(tmp_path)) == []


def test_read_chat_log_respects_limit(tmp_path):
    work = str(tmp_path)
    for i in range(10):
        RC.log_exchange(work, "ntfy", f"msg{i}", f"reply{i}")
    log = RC.read_chat_log(work, limit=3)
    assert len(log) == 3
    assert [e["text"] for e in log] == ["msg7", "msg8", "msg9"]   # the most recent


def test_chat_log_is_trimmed_once_it_grows_large(tmp_path):
    """Trimming is lazy (only pays for a rewrite once the file is meaningfully
    oversized), so the guarantee is "never grows unbounded", not "always
    exactly at the cap" — bounded by 2x keep, never trimmed away entirely."""
    work = str(tmp_path)
    total = RC._CHAT_LOG_MAX * 2 + 5
    for i in range(total):
        RC.log_exchange(work, "ntfy", f"m{i}", "r")
    with open(RC.chat_log_path(work), encoding="utf-8") as f:
        n_lines = sum(1 for _ in f)
    assert n_lines <= RC._CHAT_LOG_MAX * 2
    assert n_lines < total                                # it really did trim at some point
    log = RC.read_chat_log(work, limit=1000)
    assert log[-1]["text"] == f"m{total - 1}"              # newest survives


def test_ntfy_exchange_is_logged_and_readable_from_the_shared_log(tmp_path, monkeypatch):
    """The exact bug this fixes: a command sent from the phone must show up
    somewhere the dashboard can read it, not just be silently actioned."""
    from shortforge import netdiag, notify, ntfy_bot as NB

    work = str(tmp_path)

    class _FakeResp:
        def __init__(self, body):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    import json as _json
    payload = _json.dumps({"event": "message", "time": 1,
                           "message": "https://youtu.be/phone 4 clips"}).encode()

    class _FakeOpener:
        def open(self, req, timeout=None):
            return _FakeResp(payload)

    # poll_once imports both of these LOCALLY (`from .netdiag import build_opener`,
    # `from .notify import send_ntfy`) — patch the source modules, not ntfy_bot.
    monkeypatch.setattr(netdiag, "build_opener", lambda: _FakeOpener())
    monkeypatch.setattr(notify, "send_ntfy", lambda *a, **k: (True, "ok"))
    NB.poll_once("cmdtopic", "https://ntfy.sh", 0, work_dir=work)

    log = RC.read_chat_log(work)
    assert len(log) == 1
    assert log[0]["source"] == "ntfy"
    assert "phone" in log[0]["text"]
    assert "Queued" in log[0]["reply"]
