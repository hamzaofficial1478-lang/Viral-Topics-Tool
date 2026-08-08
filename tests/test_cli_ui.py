"""`cli.py ui` — arms its own shutdown notice, independent of `listen`'s.

The operator reported closing "the program" and getting no ntfy notification
at all, despite autostart being on. `start_all.bat` opens the dashboard
(`cli.py ui`) and the listener (`cli.py listen`) in two SEPARATE windows;
before this, only `listen`/`queue run` had any exit handling wired up, so
closing just the dashboard window produced total silence — this locks in
that `cmd_ui` now arms its own channel, using a separate state file and
wording that never claims the worker itself has stopped.
"""

import cli
from shortforge import lifecycle as L


def test_cmd_ui_arms_its_own_exit_channel(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/streamlit")
    monkeypatch.setattr("subprocess.call", lambda *a, **k: 0)

    calls = []

    def _fake_install(work_dir, **kwargs):
        calls.append((work_dir, kwargs))

    monkeypatch.setattr(L, "install_exit_notice", _fake_install)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_ui(cli.argparse.Namespace())

    assert rc == 0
    assert len(calls) == 1
    work_dir, kwargs = calls[0]
    assert kwargs["state_file"] == L.UI_STATE_FILE
    assert kwargs["include_queue_detail"] is False
    assert "dashboard" in kwargs["title"].lower()


def test_cmd_ui_refuses_cleanly_when_streamlit_is_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli.cmd_ui(cli.argparse.Namespace()) == 1
