"""STEP 3.6: dashboard renders headless; caps removed; ownership gate enforced.

The dashboard (`app.py`) is a thin Streamlit layer, exercised here via Streamlit's
headless AppTest so a broken form is caught in CI without a browser.
"""

import json

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402


def _fresh():
    return AppTest.from_file("app.py", default_timeout=30).run()


def test_new_job_screen_renders():
    at = _fresh()
    assert not at.exception
    subs = [s.value for s in at.subheader]
    assert "1. Your video" in subs and "3. What happens next" in subs
    labels = [n.label for n in at.number_input]
    assert "Clip length (seconds)" in labels and "How many clips" in labels


def test_run_is_gated_on_source_and_ownership():
    at = _fresh()
    assert at.button[0].disabled is True                      # nothing entered yet
    at.text_input[0].set_value("https://example.com/my-video").run()
    assert at.button[0].disabled is True                      # still need ownership
    at.checkbox[0].set_value(True).run()                      # owner confirmation
    assert at.button[0].disabled is False


def test_no_duration_or_clip_cap():
    at = _fresh()
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
