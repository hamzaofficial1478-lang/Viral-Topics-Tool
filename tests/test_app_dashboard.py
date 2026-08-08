"""STEP 3.6: dashboard renders headless; caps removed; ownership gate enforced.

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


def _new_job():
    """The sidebar defaults to the Queue screen now; switch to the job form."""
    at = _fresh()
    at.sidebar.radio[0].set_value("New job").run()
    return at


def test_new_job_screen_renders():
    at = _new_job()
    assert not at.exception
    subs = [s.value for s in at.subheader]
    assert "1. Your video" in subs and "3. What happens next" in subs
    labels = [n.label for n in at.number_input]
    assert "Clip length (seconds)" in labels and "How many clips" in labels


def test_run_is_gated_on_source_and_ownership():
    at = _new_job()
    assert at.button[0].disabled is True                      # nothing entered yet
    at.text_input[0].set_value("https://example.com/my-video").run()
    assert at.button[0].disabled is True                      # still need ownership
    at.checkbox[0].set_value(True).run()                      # owner confirmation
    assert at.button[0].disabled is False


def test_no_duration_or_clip_cap():
    at = _new_job()
    at.number_input[0].set_value(300).run()                   # old slider capped at 90
    at.number_input[1].set_value(35).run()                    # old number_input capped at 20
    assert not at.exception
    plan = " ".join(m.value for m in at.markdown)
    assert "300s" in plan and "**35**" in plan


def test_settings_screen_renders():
    at = _fresh()
    at.sidebar.radio[0].set_value("Settings").run()
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
    at.sidebar.radio[0].set_value("Settings").run()
    assert not at.exception
    assert "🔑 Credentials" in [s.value for s in at.subheader]


def test_settings_shows_backup_restore_section():
    at = _fresh()
    at.sidebar.radio[0].set_value("Settings").run()
    assert not at.exception
    assert "💾 Backup & restore settings" in [s.value for s in at.subheader]


def test_history_screen_renders():
    at = _fresh()
    at.sidebar.radio[0].set_value("History").run()
    assert not at.exception
    assert "📚 History" in [h.value for h in at.header]


def test_chat_screen_renders_with_a_command_input():
    """The in-dashboard chat (types the same vocabulary ntfy reads) has to at
    least render — the actual parsing is covered exhaustively against the
    shared `remote_control.handle_text` in test_remote_control.py; this only
    guards that the screen wires up without crashing."""
    at = _fresh()
    at.sidebar.radio[0].set_value("Chat").run()
    assert not at.exception
    assert "💬 Chat" in [h.value for h in at.header]
    assert len(at.chat_input) == 1


def test_job_settings_detail_matches_queue_formatting():
    """The manual New-job path's notification uses the same wording as the
    Queue/ntfy path (shared formatter), not a second copy that can drift."""
    import app
    from shortforge.config import Config

    cfg = Config.load()
    cfg.override("select.num_clips", 4)
    cfg.override("select.target_duration", 90)
    cfg.override("reframe.aspect", "9:16")
    cfg.override("reframe.resolution", "1080p")
    detail = app._job_settings_detail(cfg)
    assert "4 clip(s) of 1m30s" in detail and "9:16" in detail and "1080p" in detail


def test_manual_job_sends_ntfy_start_and_done_notifications(monkeypatch):
    """The exact bug the operator reported: starting a job from the New Job
    form (not the Queue/ntfy path) sent no notification at all — neither on
    start nor on finish."""
    import app
    import streamlit as st
    from shortforge import notify as N

    sent = []
    monkeypatch.setattr(N, "notify", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(app, "run_pipeline",
                        lambda *a, **k: {"clips": [{"file_path": "a.mp4"},
                                                    {"file_path": "b.mp4"}]})
    # _render_running() ends its "finished" branch with st.rerun(), which would
    # otherwise re-execute this whole `run()` — including _start_job() again —
    # in a loop under AppTest. We only care that the notify fired, not about
    # actually completing a second render pass, so make rerun a no-op here.
    monkeypatch.setattr(st, "rerun", lambda *a, **k: None)

    def run():
        import app
        import streamlit as st
        from shortforge.config import Config

        cfg = Config.load()
        cfg.override("select.num_clips", 2)
        app._start_job("https://youtu.be/manual-test", cfg, None, "/tmp")
        job = st.session_state.job
        job["thread"].join(timeout=5)
        app._render_running(job)          # finalizes state + fires the done/failed notice

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(run, default_timeout=15).run()
    assert not at.exception
    assert len(sent) == 2
    assert "Job started" in sent[0] and "manual-test" in sent[0]
    assert "Job done" in sent[1] and "2 clip(s)" in sent[1]


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


def test_queue_screen_is_the_default_and_renders():
    """The Queue screen is where links get pasted — it must be the landing screen."""
    at = _fresh()
    assert not at.exception
    assert "🎬 Link queue" in [h.value for h in at.header]
    subs = [s.value for s in at.subheader]
    assert "1. Add links" in subs and "2. Work through the queue" in subs
    labels = [t.label for t in at.text_area]
    assert any("links" in (l or "").lower() for l in labels)   # the paste box exists


def test_queue_add_is_gated_on_links_and_ownership():
    """No links pasted / ownership unticked -> the Add button stays disabled."""
    at = _fresh()
    add = next(b for b in at.button if "Add" in b.label)
    assert add.disabled is True
