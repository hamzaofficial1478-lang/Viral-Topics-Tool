"""Error triage: safe automatic remedies vs. human-facing diagnosis."""

from shortforge import selfheal as SH


def test_classifies_known_failures():
    assert SH.classify("ERROR: Unable to extract player response")[0] == "ytdlp_stale"
    assert SH.classify("Sign in to confirm you're not a bot")[0] == "bot_wall"
    assert SH.classify("OSError: [Errno 28] No space left on device")[0] == "disk_full"
    assert SH.classify("[WinError 10054] forcibly closed")[0] == "network"
    assert SH.classify("transcript is empty")[0] == "no_speech"
    assert SH.classify("something nobody predicted")[0] == "unknown"


def test_only_whitelisted_errors_have_automatic_remedies():
    """Anything without a safe, idempotent repair must go to a human instead."""
    for err in ("Sign in to confirm you're not a bot", "[WinError 10054]",
                "transcript is empty", "totally novel failure"):
        assert SH.classify(err)[2] is None
    assert SH.classify("unable to extract")[2] is not None    # update yt-dlp
    assert SH.classify("no space left on device")[2] is not None  # clear cache


def test_attempt_fix_reports_failure_without_raising(monkeypatch):
    """A remedy that blows up must be reported, not crash the queue."""
    boom = [(SH._RULES[0][0], SH._RULES[0][1], SH._RULES[0][2],
             lambda: (_ for _ in ()).throw(RuntimeError("pip exploded")))] + SH._RULES[1:]
    monkeypatch.setattr(SH, "_RULES", boom)
    fixed, detail = SH.attempt_fix("unable to extract")
    assert fixed is False and "failed" in detail.lower()


def test_attempt_fix_succeeds_when_remedy_works(monkeypatch):
    ok_rules = [(SH._RULES[0][0], SH._RULES[0][1], SH._RULES[0][2],
                 lambda: (True, "updated to 2026.8.1"))] + SH._RULES[1:]
    monkeypatch.setattr(SH, "_RULES", ok_rules)
    fixed, detail = SH.attempt_fix("unable to extract player response")
    assert fixed is True and "updated to" in detail


def test_report_message_shape(monkeypatch):
    monkeypatch.setattr(SH, "diagnose", lambda e, c="": "Because the sky is blue.")
    fixed, msg = SH.report("transcript is empty", context="https://x", try_fix=True)
    assert fixed is False
    assert "Job failed" in msg and "no speech" in msg.lower()
    assert "sky is blue" in msg                    # AI diagnosis included


def test_diagnosis_failure_is_not_fatal(monkeypatch):
    monkeypatch.setattr(SH, "diagnose", lambda e, c="": "")
    fixed, msg = SH.report("totally novel failure", try_fix=False)
    assert fixed is False and "novel failure" in msg   # falls back to the raw error


# --- the permission-gated auto-fix agent -------------------------------------- #
# The operator asked for an agent that reports the error and asks before fixing
# it, rather than acting on their machine unannounced.

def test_a_fixable_failure_is_proposed_not_applied(tmp_path):
    from shortforge import selfheal as SH
    offer = SH.propose("ERROR: Unable to extract player response", "job1",
                       "https://youtu.be/x", str(tmp_path))
    assert "yt-dlp" in offer
    p = SH.pending(str(tmp_path))
    assert p and p["job_id"] == "job1"


def test_an_unfixable_failure_offers_nothing(tmp_path):
    """A DRM block has no safe retry — offering one would be a false promise."""
    from shortforge import selfheal as SH
    assert SH.propose("This video is DRM protected", "job1", "u", str(tmp_path)) == ""
    assert SH.pending(str(tmp_path)) is None


def test_approving_runs_the_remedy_and_clears_it(tmp_path, monkeypatch):
    from shortforge import selfheal as SH
    monkeypatch.setattr(SH, "_RULES", [("ytdlp_stale", r"unable to extract",
                                        "stale", lambda: (True, "updated"))])
    SH.propose("Unable to extract player response", "j", "u", str(tmp_path))
    ok, detail = SH.apply_pending(str(tmp_path))
    assert ok and "updated" in detail
    assert SH.pending(str(tmp_path)) is None          # not applied twice


def test_approving_with_nothing_pending_is_harmless(tmp_path):
    from shortforge import selfheal as SH
    ok, detail = SH.apply_pending(str(tmp_path))
    assert ok is False and "no repair" in detail.lower()


def test_the_operator_can_decline(tmp_path):
    from shortforge import selfheal as SH
    SH.propose("Unable to extract player response", "j", "u", str(tmp_path))
    SH.discard_pending(str(tmp_path))
    assert SH.pending(str(tmp_path)) is None


def test_asking_first_is_the_default():
    from shortforge import selfheal as SH
    assert SH.ask_first() is True


def test_the_raw_error_is_kept_alongside_the_llm_reading(monkeypatch):
    """The LLM's diagnosis is a guess and used to REPLACE the actual error.
    A confident 'this is DRM, don't retry' over what was really a bot wall
    sends the operator to abandon a link that would have worked."""
    from shortforge import selfheal as SH
    monkeypatch.setattr(SH, "diagnose", lambda e, c="": "1. It is DRM protected.")
    _fixed, msg = SH.report("ERROR: Sign in to confirm you're not a bot", "u",
                            try_fix=False)
    assert "DRM protected" in msg                      # the reading is shown
    assert "not a bot" in msg                          # and so is the evidence
    assert "Actual error" in msg


# --- whose fault is it: the link, the machine, or ShortForge? ------------------ #
# The operator's question on every failure. Lumping them together meant "this
# video is undownloadable" and "the program has a bug" read identically, and
# they need opposite responses.

import pytest as _pt


@_pt.mark.parametrize("err", [
    "This video is DRM protected", "Private video. Sign in", "Video unavailable",
    "Sign in to confirm you're not a bot", "no detectable speech in this source",
    "ERROR: Requested format is not available",
])
def test_link_side_failures_are_named_as_the_video_not_the_program(err):
    from shortforge import selfheal as SH
    assert SH.origin(err) == SH.LINK
    msg = SH.where_and_what(err)
    assert "this video, not the program" in msg
    assert "Nothing to fix" in msg


@_pt.mark.parametrize("err", [
    "[Errno 28] No space left on device", "ffmpeg: not found on PATH",
    "WinError 10054 forcibly closed", "Unable to extract player response",
])
def test_machine_side_failures_are_named_as_setup(err):
    from shortforge import selfheal as SH
    assert SH.origin(err) == SH.MACHINE
    assert "machine's setup" in SH.where_and_what(err)


@_pt.mark.parametrize("err", [
    "TypeError: 'NoneType' object is not subscriptable",
    "KeyError: 'clips'", "IndexError: list index out of range",
])
def test_unrecognised_failures_are_treated_as_a_bug_in_shortforge(err):
    from shortforge import selfheal as SH
    assert SH.origin(err) == SH.PIPELINE
    msg = SH.where_and_what(err)
    assert "inside ShortForge itself" in msg
    assert "will <b>not</b> change the program" in msg


def test_a_suspected_program_bug_is_never_offered_an_automatic_repair(tmp_path):
    """The line the operator drew: the agent must not touch pipeline code, no
    matter how confident any model is about the cause."""
    from shortforge import selfheal as SH
    assert SH.propose("TypeError: 'NoneType' object is not subscriptable",
                      "j", "u", str(tmp_path)) == ""
    assert SH.pending(str(tmp_path)) is None


def test_a_link_side_failure_is_never_offered_a_repair_either(tmp_path):
    from shortforge import selfheal as SH
    assert SH.propose("This video is DRM protected", "j", "u", str(tmp_path)) == ""


def test_only_machine_problems_get_an_offer(tmp_path):
    from shortforge import selfheal as SH
    assert SH.propose("[Errno 28] No space left on device", "j", "u", str(tmp_path)) != ""
