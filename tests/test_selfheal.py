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
