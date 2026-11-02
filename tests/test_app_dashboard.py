"""The dashboard renders headless; one link list; caps removed; ownership gate enforced.

The dashboard (`app.py`) is a thin Streamlit layer, exercised here via Streamlit's
headless AppTest so a broken form is caught in CI without a browser.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

# Absolute, not "app.py": AppTest.from_file resolves a relative path against the
# CALLING file's directory (tests/), not the CWD — a bare "app.py" looks for
# tests/app.py and raises FileNotFoundError regardless of where pytest is run from.
_APP = str(Path(__file__).resolve().parent.parent / "app.py")


def _fresh():
    return AppTest.from_file(_APP, default_timeout=30).run()


@pytest.fixture
def wd(tmp_path, monkeypatch):
    """Run in an empty folder, so the link list starts empty and stays out of the repo."""
    monkeypatch.chdir(tmp_path)
    return tmp_path / ".shortforge"


def _btn(at, starts: str):
    return next(b for b in at.button if b.label.startswith(starts))


def _jobs(wd):
    from shortforge import queue as Q
    return Q.load_queue(str(wd))["jobs"]


def _no_real_worker(monkeypatch) -> list:
    """The app script runs as its own module under AppTest, so patch the shared
    modules it calls — never let a test start a real `cli.py queue run`."""
    from shortforge import lifecycle, runner
    spawned = []
    monkeypatch.setattr(runner, "spawn_worker", lambda root: spawned.append(1))
    monkeypatch.setattr(lifecycle, "worker_is_live", lambda *a, **k: False)
    return spawned


def _paste(at, text: str, *, owner: bool = True):
    at.text_area[0].set_value(text).run()
    if owner:
        at.checkbox(key="lv_owner").check().run()
    return at


def test_long_videos_is_the_default_screen_with_one_add_section_and_one_list(wd):
    at = _fresh()
    assert not at.exception
    assert [h.value for h in at.header] == ["📹 Long videos → clips"]
    assert [s.value for s in at.subheader] == ["1. Add videos", "2. Your videos"]
    assert any("links" in (t.label or "").lower() for t in at.text_area)


def test_logo_removal_tick_box_is_on_screen_not_tucked_in_an_expander(wd):
    """The operator couldn't find it: it used to sit inside a closed Advanced
    expander of one form, and did nothing anyway."""
    at = _fresh()
    logo = at.checkbox(key="lv_logo")
    assert "Remove logos" in logo.label
    for exp in at.expander:
        assert "lv_logo" not in [c.key for c in exp.checkbox]


def test_add_is_gated_on_links_and_ownership(wd):
    at = _fresh()
    assert not [b for b in at.button if "Add" in b.label]       # nothing pasted yet
    _paste(at, "https://youtu.be/one\nhttps://youtu.be/two", owner=False)
    assert _btn(at, "➕ Add 2 videos").disabled is True
    at.checkbox(key="lv_owner").check().run()
    assert _btn(at, "➕ Add 2 videos").disabled is False


def test_one_link_can_be_processed_straight_away_many_go_to_the_queue(wd):
    at = _paste(_fresh(), "https://youtu.be/only-one")
    assert {b.label for b in at.button} >= {"▶ Process this video now", "➕ Add to the queue"}
    at = _paste(_fresh(), "https://youtu.be/a\nhttps://youtu.be/b\nhttps://youtu.be/c")
    assert [b.label for b in at.button if "Add" in b.label or "now" in b.label] == \
        ["➕ Add 3 videos to the queue"]


def test_added_videos_wait_for_process_and_keep_their_settings(wd):
    from shortforge import queue as Q
    at = _paste(_fresh(), "https://youtu.be/a\nhttps://youtu.be/b")
    at.number_input(key="lv_clips").set_value(35).run()        # no cap on clips…
    at.number_input(key="lv_dur").set_value(300).run()         # …or on length
    at.checkbox(key="lv_logo").check().run()
    _btn(at, "➕ Add 2").click().run()
    assert not at.exception
    jobs = _jobs(wd)
    assert [j["status"] for j in jobs] == [Q.LISTED, Q.LISTED]
    assert jobs[0]["settings"]["num_clips"] == 35 and jobs[0]["settings"]["duration"] == 300
    assert jobs[0]["settings"]["remove_logos"] is True
    assert Q.next_pending(Q.load_queue(str(wd))) is None         # nothing runs by itself
    assert at.text_area[0].value in ("", None)                  # the box is cleared


def test_advanced_options_from_the_old_form_are_still_reachable(wd):
    at = _paste(_fresh(), "https://youtu.be/advanced")
    at.selectbox(key="lv_cam").set_value("Follow the speaker").run()
    at.selectbox(key="lv_enc").set_value("qsv").run()
    at.selectbox(key="lv_whisper").set_value("medium").run()
    at.selectbox(key="lv_anim").set_value("karaoke").run()
    at.text_input(key="lv_srclang").set_value("fr").run()
    at.checkbox(key="lv_jump").check().run()
    _btn(at, "➕ Add to the queue").click().run()
    s = _jobs(wd)[0]["settings"]
    assert {k: s[k] for k in ("reframe_mode", "encoder", "whisper_model", "caption_animation",
                              "source_language", "jumpcuts")} == {
        "reframe_mode": "track", "encoder": "qsv", "whisper_model": "medium",
        "caption_animation": "karaoke", "source_language": "fr", "jumpcuts": True}
    from shortforge import queue as Q
    from shortforge.config import Config
    cfg = Config.load()
    Q.apply_job_settings(cfg, _jobs(wd)[0])
    assert (cfg.get("reframe.mode"), cfg.get("render.encoder"), cfg.get("transcribe.model"),
            cfg.get("transcribe.language"), cfg.get("edit.jumpcuts")) == \
        ("track", "qsv", "medium", "fr", True)


def test_a_link_already_on_the_list_is_not_added_again(wd):
    at = _paste(_fresh(), "https://youtu.be/same-video")
    _btn(at, "➕ Add to the queue").click().run()
    _paste(at, "https://www.youtube.com/watch?v=same-video&si=share")
    _btn(at, "➕ Add to the queue").click().run()
    assert len(_jobs(wd)) == 1
    assert any("already in the list" in w.value for w in at.warning)


def test_process_now_adds_and_starts_one_video(wd, monkeypatch):
    from shortforge import queue as Q
    spawned = _no_real_worker(monkeypatch)
    at = _paste(_fresh(), "https://youtu.be/go-now")
    _btn(at, "▶ Process this video now").click().run()
    assert not at.exception
    assert [j["status"] for j in _jobs(wd)] == [Q.PENDING]
    assert spawned == [1]


def test_process_all_starts_waiting_and_failed_only(wd, monkeypatch):
    from shortforge import queue as Q
    spawned = _no_real_worker(monkeypatch)
    q = Q.load_queue(str(wd))
    for url, status in (("https://youtu.be/w1", Q.LISTED), ("https://youtu.be/w2", Q.LISTED),
                        ("https://youtu.be/f1", Q.FAILED), ("https://youtu.be/d1", Q.DONE)):
        Q.add_job(q, url, {}, status=status)
    Q.save_queue(q, str(wd))
    at = _fresh()
    assert _btn(at, "▶ Process selected (0)").disabled is True   # nothing ticked
    _btn(at, "▶ Process all (3)").click().run()
    assert [j["status"] for j in _jobs(wd)] == [Q.PENDING, Q.PENDING, Q.PENDING, Q.DONE]
    assert spawned == [1]


def test_ticked_rows_and_table_edits(wd):
    import app
    from shortforge import queue as Q
    q = Q.load_queue(str(wd))
    a = Q.add_job(q, "https://youtu.be/a", {"num_clips": 3}, status=Q.LISTED)
    b = Q.add_job(q, "https://youtu.be/b", {"num_clips": 3}, status=Q.RUNNING)
    Q.save_queue(q, str(wd))
    ids = [a["id"], b["id"]]
    assert app.selected_ids(ids, {0: {"✓": True}, 1: {"Clips": 5}, 7: {"✓": True}}) == [a["id"]]
    saved, refused = app.apply_link_edits(str(wd), ids, {
        0: {"Clips": 6, "Seconds": 2, "Shape": "9:16", "🧽 Logo": True},
        1: {"Clips": 9}})                                       # running: locked
    assert (saved, refused) == (1, 1)
    jobs = {j["id"]: j for j in _jobs(wd)}
    assert jobs[a["id"]]["settings"] == {"num_clips": 6, "duration": 5, "aspect": "9:16",
                                         "remove_logos": True}
    assert jobs[b["id"]]["settings"] == {"num_clips": 3}


def test_settings_screen_renders():
    at = _fresh()
    at.sidebar.radio[0].set_value("⚙️ Settings").run()
    assert not at.exception


def test_settings_shows_credentials_with_a_seeded_model(tmp_path, monkeypatch):
    """R6: a credential holding a model renders on the Settings screen."""
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))
    from shortforge.providers import store as S
    store = {"providers": [], "credentials": []}
    cred = S.add_credential(store, name="AgentRouter",
                            base_url="https://agentrouter.org/v1", api_key="ar-xyz")
    S.add_model(store, cred["id"], model="minimaxai/minimax-m3", category="llm")
    S.save_store(store)

    at = _fresh()
    at.sidebar.radio[0].set_value("⚙️ Settings").run()
    assert not at.exception
    assert "🔑 Credentials" in [s.value for s in at.subheader]


def _settings_tab(label: str):
    """Settings opens on its first tab; only the open tab is built. (The test
    tool can't click a tab, so the choice is set the way a click sets it.)"""
    at = _fresh()
    at.sidebar.radio[0].set_value("⚙️ Settings").run()
    at.session_state["set_tab"] = label
    return at.run()


def test_settings_shows_backup_restore_section():
    at = _settings_tab("💾 Backup & restore")
    assert not at.exception
    assert "💾 Backup & restore settings" in [s.value for s in at.subheader]


def test_settings_builds_only_the_open_section(monkeypatch):
    """Settings was one long page rebuilt on every click — including measuring
    every file in the working folder. Now that only happens on its own tab."""
    from shortforge import maintenance as M
    from shortforge.ui import settings as SET
    scans = []
    real = M.cache_report
    monkeypatch.setattr(M, "cache_report", lambda wd: (scans.append(wd), real(wd))[1])
    SET._disk_report.clear()
    at = _fresh()
    at.sidebar.radio[0].set_value("⚙️ Settings").run()
    assert not at.exception and scans == []
    assert "🔑 Credentials" in [s.value for s in at.subheader]
    assert "💾 Backup & restore settings" not in [s.value for s in at.subheader]
    at = _settings_tab("🧹 Disk & upkeep")
    assert not at.exception and len(scans) == 1
    assert "🧹 Disk space" in [s.value for s in at.subheader]


def test_history_screen_renders():
    at = _fresh()
    at.sidebar.radio[0].set_value("📚 History").run()
    assert not at.exception
    assert "📚 History" in [h.value for h in at.header]


def test_stage_from_lines_tracks_furthest_progress():
    import app
    frac0, label0 = app._stage_from_lines(["ingesting source video"])
    frac1, label1 = app._stage_from_lines(
        ["ingesting source video", "transcribing audio", "rendering clip 01"])
    assert label0 == "Ingesting source"
    assert frac1 > frac0 and label1 == "Rendering"           # furthest stage wins
    # unknown lines don't crash and report a small non-zero fraction
    frac2, _ = app._stage_from_lines(["something unrelated"])
    assert 0.0 < frac2 < 0.2


def test_render_clip_metadata_helpers_are_pure():
    """The results helpers used by both live + history views import cleanly and
    the copyable-field helper no-ops on empty text (no Streamlit context needed)."""
    import app
    # _copyable must tolerate empty text without touching Streamlit widgets.
    app._copyable("Title", "", "k")                          # returns early, no error


def test_import_store_round_trips_keys_and_routing(tmp_path, monkeypatch):
    """Export → import restores credentials (keys), models and task bindings intact."""
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))
    from shortforge.providers import store as S
    from shortforge.ui import settings as UI

    src = {"providers": [], "credentials": [], "tasks": {}}
    cred = S.add_credential(src, name="NVIDIA build",
                            base_url="https://integrate.api.nvidia.com/v1", api_key="nvapi-secret")
    m = S.add_model(src, cred["id"], model="minimaxai/minimax-m3", category="llm")
    S.set_task_binding(src, "hook_detection", primary=m["id"])

    exported = json.loads(json.dumps(src))              # what the download_button emits
    monkeypatch.setattr(UI, "_persist", lambda store: S.save_store(store))
    UI._import_store(exported)                            # what an upload does

    restored = S.load_store()
    assert restored["credentials"][0]["api_key"] == "nvapi-secret"   # key preserved
    assert restored["credentials"][0]["models"][0]["model"] == "minimaxai/minimax-m3"
    assert restored["tasks"]["hook_detection"]["primary"] == m["id"]  # routing preserved


def test_import_store_rejects_junk():
    from shortforge.ui import settings as UI
    import pytest as _pytest
    assert UI._valid_store({"credentials": []}) is True
    assert UI._valid_store({"nonsense": 1}) is False
    with _pytest.raises(ValueError):
        UI._import_store({"nonsense": 1})


def test_queue_screen_shows_live_progress_for_a_running_job(tmp_path, monkeypatch):
    """The bug the operator reported: a job started from ntfy (running in a
    separate process, cli.py listen) showed no progress on the Queue screen
    at all — unlike the manual New Job form. This is the fix: the Queue
    screen now tails the same per-job log file that process writes."""
    import os
    from shortforge import queue as Q
    from shortforge.runner import job_log_path

    monkeypatch.chdir(tmp_path)
    work_dir = ".shortforge"
    os.makedirs(work_dir, exist_ok=True)
    q = Q.load_queue(work_dir)
    Q.add_job(q, "https://youtu.be/running-now", {"num_clips": 2})
    Q.mark(q, q["jobs"][0]["id"], Q.RUNNING)
    Q.save_queue(q, work_dir)
    with open(job_log_path(work_dir), "w", encoding="utf-8") as f:
        f.write("ingesting source video\ntranscribing audio\n")

    at = _fresh()

    assert not at.exception
    assert len(at.get("progress")) == 1     # not a typed AppTest element, just present
    assert any("running-now" in c.value for c in at.caption)
    log_text = "\n".join(c.value for c in at.code)
    assert "transcribing audio" in log_text


def test_queue_screen_surfaces_warnings_and_elapsed_time_like_new_job_does(tmp_path, monkeypatch):
    """The operator asked for the queue-driven running view to show the same
    level of detail the manual New Job form always has — live log, warnings
    called out separately, elapsed time — not just a bare progress bar, so a
    real problem (not just slowness) can actually be spotted from this screen."""
    import os
    from shortforge import lifecycle, queue as Q
    from shortforge.runner import job_log_path

    monkeypatch.chdir(tmp_path)
    work_dir = ".shortforge"
    os.makedirs(work_dir, exist_ok=True)
    q = Q.load_queue(work_dir)
    Q.add_job(q, "https://youtu.be/warn-case", {"num_clips": 2})
    Q.mark(q, q["jobs"][0]["id"], Q.RUNNING)
    Q.save_queue(q, work_dir)
    with open(job_log_path(work_dir), "w", encoding="utf-8") as f:
        f.write("INFO\tingesting source video\n"
                "WARNING\tvision scoring returned HTTP 400, skipping frame fusion\n"
                "INFO\ttranscribing audio\n")
    lifecycle.mark_online(work_dir)
    lifecycle.heartbeat(work_dir, current={"url": "https://youtu.be/warn-case",
                                           "detail": "2 clip(s)", "index": 1, "total": 1,
                                           "started": __import__("time").time() - 42})

    at = _fresh()

    assert not at.exception
    # elapsed time is now part of the progress bar's own text, not a separate widget
    progress_texts = " ".join(str(getattr(p, "text", "") or "") for p in at.get("progress"))
    assert "elapsed" in progress_texts
    assert any("Link 1/1" in c.value for c in at.caption)
    warn_headers = [e.label for e in at.expander if "warning" in (e.label or "").lower()]
    assert len(warn_headers) == 1 and "1 warning" in warn_headers[0]
    body = at.expander[0]
    warning_widgets = [w.value for w in body.warning] if hasattr(body, "warning") else []
    assert any("HTTP 400" in w for w in warning_widgets) or any(
        "HTTP 400" in w.value for w in at.warning)


# --- "▶ Start working" ------------------------------------------------------- #
# The operator's report: pressing it produced three phone messages at once
# ("ShortForge started" / "All links processed" / "ShortForge stopped") while
# the queued link stayed pending and never began. Cause: the un-pause was
# written to disk AFTER the worker process was spawned, so the child read
# queue.json while it still said paused:true and bailed out instantly.

def test_start_working_unpauses_on_disk_before_spawning_the_worker(tmp_path, monkeypatch):
    """The ordering IS the bug. What the spawned process reads at the moment it
    starts must already say the queue is running."""
    import app
    from shortforge import queue as Q

    work_dir = str(tmp_path)
    q = Q.load_queue(work_dir)
    Q.add_job(q, "https://youtu.be/must-actually-start")
    Q.set_paused(q, True)                       # the permission gate left it paused
    Q.save_queue(q, work_dir)

    seen_by_worker = {}

    def _fake_spawn():
        # Exactly what `cli.py queue run` does first: read queue.json off disk.
        seen_by_worker["paused"] = Q.is_paused(Q.load_queue(work_dir))

    monkeypatch.setattr(app, "_start_worker", _fake_spawn)
    monkeypatch.setattr(app, "_worker_is_live", lambda *a, **k: False)

    app._begin_working(work_dir)

    assert seen_by_worker["paused"] is False    # was False BEFORE the spawn
    assert Q.is_paused(Q.load_queue(work_dir)) is False


def test_start_working_does_not_spawn_a_second_worker_when_one_is_live(tmp_path, monkeypatch):
    """`start_all.bat` already runs `cli.py listen`, whose worker thread drains
    the queue on its own. Spawning `cli.py queue run` on top of it produced a
    redundant process that fought for the lock, overwrote the listener's
    runstate.json, and sent its own start/finish/shutdown messages."""
    import app
    from shortforge import queue as Q

    work_dir = str(tmp_path)
    q = Q.load_queue(work_dir)
    Q.add_job(q, "https://youtu.be/already-have-a-worker")
    Q.set_paused(q, True)
    Q.save_queue(q, work_dir)

    spawned = []
    monkeypatch.setattr(app, "_start_worker", lambda: spawned.append(1))
    monkeypatch.setattr(app, "_worker_is_live", lambda *a, **k: True)

    msg = app._begin_working(work_dir)

    assert spawned == []                                  # no competing process
    assert Q.is_paused(Q.load_queue(work_dir)) is False    # but it IS unpaused
    assert "picks this up" in msg


def test_worker_is_live_tracks_the_heartbeat(tmp_path):
    import app
    from shortforge import lifecycle

    work_dir = str(tmp_path)
    assert app._worker_is_live(work_dir) is False          # nothing running
    lifecycle.mark_online(work_dir, "listen")
    assert app._worker_is_live(work_dir) is True           # fresh heartbeat
    state = lifecycle.read_state(work_dir)
    state["last_seen"] = state["last_seen"] - 600          # gone quiet
    lifecycle._write_state(work_dir, state)
    assert app._worker_is_live(work_dir) is False


# --- removing a link from the queue must always notify ----------------------- #
# Operator: added 10 links, and whenever one leaves the queue (however it
# leaves) expects to hear about it on the phone — previously only /cancel and
# /clear sent via ntfy itself did (via ntfy_bot's own send_ntfy reply); the
# dashboard's own "Clear finished" and "Remove this link" buttons were silent.

def test_clear_finished_button_notifies(tmp_path, monkeypatch):
    import os
    from shortforge import notify as N
    from shortforge import queue as Q

    monkeypatch.chdir(tmp_path)
    work_dir = ".shortforge"
    os.makedirs(work_dir, exist_ok=True)
    q = Q.load_queue(work_dir)
    Q.add_job(q, "https://youtu.be/finished-one")
    Q.mark(q, q["jobs"][0]["id"], Q.DONE, clips=["a.mp4"])
    Q.save_queue(q, work_dir)

    sent = []
    monkeypatch.setattr(N, "notify", lambda text, **k: sent.append(text) or True)

    at = _fresh()
    btn = next(b for b in at.button if "Clear finished" in b.label)
    btn.click().run()

    assert not at.exception
    assert len(sent) == 1 and "Removed 1" in sent[0]
    assert Q.load_queue(work_dir)["jobs"] == []


def test_removing_links_notifies_and_keeps_the_one_working(wd, monkeypatch):
    import app
    from shortforge import notify as N
    from shortforge import queue as Q
    q = Q.load_queue(str(wd))
    a = Q.add_job(q, "https://youtu.be/remove-me-please", status=Q.LISTED)
    b = Q.add_job(q, "https://youtu.be/working-now", status=Q.RUNNING)
    Q.save_queue(q, str(wd))
    sent = []
    monkeypatch.setattr(N, "notify", lambda text, **k: sent.append(text) or True)

    def run(ids):
        import app as _app
        _app._remove(".shortforge", set(ids))

    at = AppTest.from_function(run, args=([a["id"], b["id"]],), default_timeout=15).run()
    assert not at.exception
    assert [j["url"] for j in _jobs(wd)] == ["https://youtu.be/working-now"]
    assert len(sent) == 1 and "remove-me-please" in sent[0]


def _past_runs(root: Path, n_runs: int, n_clips: int = 3) -> None:
    for r in range(n_runs):
        d = root / "out" / "someone" / f"vid{r:03d}"
        d.mkdir(parents=True)
        clips = []
        for c in range(n_clips):
            fp = d / f"clip_{c}.mp4"
            fp.write_bytes(b"\0" * 1024)
            clips.append({"clip_id": c + 1, "file_path": str(fp), "start": 0, "end": 30,
                          "score": 0.7, "metadata": {"title": f"Run {r} clip {c}"}})
        (d / f"vid{r:03d}_manifest.json").write_text(
            json.dumps({"source": {"title": f"Run {r}"}, "clips": clips, "summary": "done"}),
            encoding="utf-8")


def test_history_reads_no_clip_until_play_is_pressed(tmp_path, monkeypatch):
    """The History screen drew every run ever made, each clip with a player and
    a download button that BOTH read the whole video file — on every click.
    Now: one run at a time, downloads read on click, players load on ▶ Play."""
    import builtins
    _past_runs(tmp_path, n_runs=4)
    monkeypatch.chdir(tmp_path)
    reads = []
    real_open = builtins.open

    def spy(f, mode="r", *a, **k):
        if "b" in mode and str(f).endswith(".mp4"):
            reads.append(str(f))
        return real_open(f, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)

    at = _fresh()
    at.sidebar.radio[0].set_value("📚 History").run()
    assert not at.exception
    assert len(at.selectbox[0].options) == 4
    assert [b.label for b in at.button].count("▶ Play") == 3      # one run's clips only
    assert not at.get("video") and reads == []
    at.button[1].click().run()                                     # first ▶ Play
    assert not at.exception
    assert len(at.get("video")) == 1 and len(reads) == 1


def test_settings_leads_with_one_actionable_note_not_four_warnings(tmp_path, monkeypatch):
    """Operator's screenshot: four yellow warnings on every Settings visit, three
    of them about dubbing. Now: one note (no AI model) with what to do; the
    dubbing limits sit in a closed panel under Models & keys."""
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "providers.local.json"))
    at = _fresh()
    at.sidebar.radio[0].set_value("⚙️ Settings").run()
    assert not at.exception
    assert not [w for w in at.warning if "voice provider" in w.value]
    assert any("No AI model is set up" in i.value and "Backup & restore" in i.value
               for i in at.info)
    assert any("Dubbing:" in e.label for e in at.expander)
