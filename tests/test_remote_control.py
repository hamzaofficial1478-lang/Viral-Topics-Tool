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


# --- /status must answer "will this start, or must I send something?" --------- #
# The operator has had to ask that question every round. /status used to print
# counts plus a fixed "is start_all.bat still open?" line -- emitted identically
# whether a worker was running or not, because it never checked.

def _queued(work, *, paused=False, running=False):
    q = Q.load_queue(work)
    Q.add_job(q, "https://youtu.be/AAAAAAAAAAA")
    if running:
        Q.mark(q, q["jobs"][0]["id"], Q.RUNNING)
    Q.set_paused(q, paused)
    Q.save_queue(q, work)


def test_status_says_open_start_all_when_no_worker_is_running(tmp_path):
    """The queue is only a list until something reads it."""
    work = str(tmp_path)
    _queued(work)
    s = RC.handle_text("/status", work)
    assert "not running" in s and "start_all.bat" in s
    assert "Reply <b>start</b>" not in s          # sending start would not help


def test_status_says_reply_start_when_the_permission_gate_is_holding(tmp_path):
    from shortforge import lifecycle
    work = str(tmp_path)
    _queued(work, paused=True)
    lifecycle.mark_online(work, "listen")
    s = RC.handle_text("/status", work)
    assert "Reply <b>start</b>" in s and "permission gate" in s
    assert "start_all.bat" not in s               # the worker IS running


def test_status_says_nothing_to_send_when_it_will_start_on_its_own(tmp_path):
    """The case behind the recurring question: unpaused with a live worker means
    it begins by itself, and the operator should be told to just wait."""
    from shortforge import lifecycle
    work = str(tmp_path)
    _queued(work)
    lifecycle.mark_online(work, "listen")
    s = RC.handle_text("/status", work)
    assert "Nothing to send" in s
    assert "Reply <b>start</b>" not in s


def test_status_reports_a_running_job_instead_of_diagnosing(tmp_path):
    from shortforge import lifecycle
    work = str(tmp_path)
    _queued(work, running=True)
    lifecycle.mark_online(work, "listen")
    s = RC.handle_text("/status", work)
    assert "Now:" in s and "it's working" in s


def test_status_on_an_empty_queue_is_not_alarming(tmp_path):
    from shortforge import lifecycle
    work = str(tmp_path)
    lifecycle.mark_online(work, "listen")
    s = RC.handle_text("/status", work)
    assert "Nothing waiting" in s


def test_status_always_states_whether_a_worker_exists(tmp_path):
    """The single fact that decides whether anything can happen at all."""
    from shortforge import lifecycle
    work = str(tmp_path)
    _queued(work)
    assert "🔴 not running" in RC.handle_text("/status", work)
    lifecycle.mark_online(work, "listen")
    assert "🟢 running" in RC.handle_text("/status", work)


def test_the_phone_and_the_dashboard_agree_on_whether_a_worker_exists(tmp_path):
    """One implementation, so /status and the Start button can never contradict
    each other about the same machine."""
    import app
    from shortforge import lifecycle
    work = str(tmp_path)
    assert app._worker_is_live(work) is lifecycle.worker_is_live(work) is False
    lifecycle.mark_online(work, "listen")
    assert app._worker_is_live(work) is lifecycle.worker_is_live(work) is True


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
    """The silent-stall shape. With no worker, "start" looks like it worked and
    nothing happens — the phone must be able to tell that apart from progress.
    (Cause-specific wording is covered by the /status tests above.)"""
    work = str(tmp_path)
    RC.handle_text("https://a/1", work)
    RC.handle_text("start", work)

    status = RC.handle_text("/status", work)

    assert "not running" in status
    assert "👉" in status              # always ends in a concrete next step


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


# --- one message, many links, each with its own settings ---------------------- #
# Settings were parsed from the WHOLE message and applied to every link in it,
# so the obvious way to send a batch silently mangled it: line 2's "portrait"
# and line 3's "720p" leaked onto line 1, and line 2's clip count vanished.

def test_each_line_keeps_its_own_settings(tmp_path):
    work = str(tmp_path)
    RC.handle_text("https://youtu.be/AAAAAAAAAAA 3 clips of 60s\n"
                   "https://youtu.be/BBBBBBBBBBB 6 clips of 2min portrait\n"
                   "https://youtu.be/CCCCCCCCCCC 2 clips 720p", work)
    by_url = {j["url"][-11:]: j["settings"] for j in Q.load_queue(work)["jobs"]}
    assert by_url["AAAAAAAAAAA"] == {"num_clips": 3, "duration": 60}
    assert by_url["BBBBBBBBBBB"] == {"num_clips": 6, "duration": 120, "aspect": "9:16"}
    assert by_url["CCCCCCCCCCC"] == {"num_clips": 2, "resolution": "720p"}


def test_a_settings_only_line_acts_as_a_default_for_the_links_below(tmp_path):
    work = str(tmp_path)
    RC.handle_text("3 clips of 60s portrait\n"
                   "https://youtu.be/DDDDDDDDDDD\n"
                   "https://youtu.be/EEEEEEEEEEE 6 clips", work)
    by_url = {j["url"][-11:]: j["settings"] for j in Q.load_queue(work)["jobs"]}
    assert by_url["DDDDDDDDDDD"] == {"num_clips": 3, "duration": 60, "aspect": "9:16"}
    # the line's own value wins over the header, the rest is inherited
    assert by_url["EEEEEEEEEEE"] == {"num_clips": 6, "duration": 60, "aspect": "9:16"}


def test_several_links_on_one_line_still_share_that_line(tmp_path):
    work = str(tmp_path)
    RC.handle_text("https://youtu.be/FFFFFFFFFFF https://youtu.be/GGGGGGGGGGG 4 clips", work)
    for j in Q.load_queue(work)["jobs"]:
        assert j["settings"]["num_clips"] == 4


def test_the_reply_lists_what_it_understood_for_each_link(tmp_path):
    """With per-link settings, one merged summary would hide a mistake on line
    3 of ten — so each link echoes its own."""
    reply = RC.handle_text("https://youtu.be/HHHHHHHHHHH 3 clips\n"
                           "https://youtu.be/IIIIIIIIIII 7 clips", str(tmp_path))
    assert "3 clip(s)" in reply and "7 clip(s)" in reply
    assert "HHHHHHHHHHH" in reply and "IIIIIIIIIII" in reply


# --- asking for titles/descriptions from the phone ---------------------------- #
# "metadata" was not on the queue.JOB_SETTINGS whitelist, so the request was
# parsed and then dropped without a word: no metadata was produced and nothing
# said why. Silently ignoring what the operator asked for is the failure mode
# this project exists to avoid.

@pytest.mark.parametrize("phrase", [
    "with title and description", "metadata=on", "seo=yes", "titles please",
])
def test_asking_for_titles_is_accepted(tmp_path, phrase):
    work = str(tmp_path)
    RC.handle_text(f"https://youtu.be/AAAAAAAAAAA 3 clips {phrase}", work)
    assert Q.load_queue(work)["jobs"][0]["settings"].get("metadata") is True


def test_declining_titles_is_also_understood(tmp_path):
    work = str(tmp_path)
    RC.handle_text("https://youtu.be/BBBBBBBBBBB 3 clips no title", work)
    assert Q.load_queue(work)["jobs"][0]["settings"].get("metadata") is False


def test_the_reply_confirms_titles_were_understood(tmp_path):
    reply = RC.handle_text("https://youtu.be/CCCCCCCCCCC 2 clips with title and description",
                           str(tmp_path))
    assert "title/description" in reply


def test_metadata_reaches_the_pipeline_config(tmp_path):
    """On the whitelist means apply_job_settings actually turns the stage on."""
    from shortforge.config import Config
    work = str(tmp_path)
    RC.handle_text("https://youtu.be/DDDDDDDDDDD 2 clips with title and description", work)
    cfg = Config.load()
    assert cfg.get("metadata.enabled") is False           # off by default
    Q.apply_job_settings(cfg, Q.load_queue(work)["jobs"][0])
    assert cfg.get("metadata.enabled") is True


def test_titles_are_sent_to_the_phone_when_they_were_asked_for():
    """A title sitting in a manifest on the PC is no use to someone posting
    from their phone."""
    from shortforge import runner
    job = {"settings": {"metadata": True}}
    manifest = {"clips": [{"clip_id": "01", "metadata": {
        "title": "The Pirate Captain", "description": "How a slave became captain.",
        "hashtags": ["#pirates"]}}]}
    out = runner._metadata_lines(manifest, job)
    assert "The Pirate Captain" in out and "How a slave became captain." in out
    assert "#pirates" in out


def test_nothing_extra_is_sent_when_titles_were_not_asked_for():
    from shortforge import runner
    assert runner._metadata_lines({"clips": [{"metadata": {"title": "x"}}]},
                                  {"settings": {}}) == ""


def test_asking_for_titles_and_getting_none_is_reported_not_hidden():
    from shortforge import runner
    out = runner._metadata_lines({"clips": [{"clip_id": "01"}]}, {"settings": {"metadata": True}})
    assert "requested but none were produced" in out
