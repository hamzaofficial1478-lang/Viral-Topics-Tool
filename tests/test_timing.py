"""STEP 0 — per-stage + per-ffmpeg timing collector."""

from shortforge import timing


def test_no_op_when_inactive():
    # No active Timings → stage/note/record are cheap no-ops that never raise.
    with timing.stage("render"):
        pass
    timing.note("render", "skipped")
    timing.record_ffmpeg(["ffmpeg", "-i", "x"], 1.0)
    assert timing.current() is None


def test_stage_accumulates_by_name():
    t = timing.Timings()
    tok = timing.activate(t)
    try:
        with timing.stage("render"):
            pass
        with timing.stage("render"):
            pass
        t.add("render", 4.0)                       # direct add on top of two timed blocks
    finally:
        timing.deactivate(tok)
    render = [s for s in t.stages if s["stage"] == "render"][0]
    assert render["seconds"] >= 4.0               # accumulated, one row (not three)
    assert len([s for s in t.stages if s["stage"] == "render"]) == 1


def test_note_marks_skipped_stage():
    t = timing.Timings()
    tok = timing.activate(t)
    try:
        timing.note("stems", "skipped")
    finally:
        timing.deactivate(tok)
    line = t.summary_line()
    assert "stems skipped" in line
    assert line.startswith("timings:") and "total" in line


def test_records_ffmpeg_calls_and_serializes():
    t = timing.Timings()
    tok = timing.activate(t)
    try:
        t.add("ingest", 12.0)
        timing.record_ffmpeg(["ffmpeg", "-y", "-i", "in.mp4", "out.mp4"], 3.2)
    finally:
        timing.deactivate(tok)
    d = t.to_dict()
    assert d["stages"][0] == {"stage": "ingest", "seconds": 12.0, "note": None}
    assert d["ffmpeg_calls"][0]["seconds"] == 3.2
    assert "ffmpeg -y -i in.mp4 out.mp4" == d["ffmpeg_calls"][0]["cmd"]
    assert "ingest 12s" in t.summary_line()


def test_cached_note_annotates_a_timed_stage():
    t = timing.Timings()
    tok = timing.activate(t)
    try:
        t.add("asr", 0.0)
        timing.note("asr", "cached")
    finally:
        timing.deactivate(tok)
    assert "asr 0s (cached)" in t.summary_line()
