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
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "streamlit" else real(name, *a))
    assert cli.cmd_ui(cli.argparse.Namespace()) == 1


def test_cmd_ui_runs_streamlit_through_this_python(monkeypatch, tmp_path):
    """Not the `streamlit` program on PATH: that one has the venv's original
    location written into it and breaks once the folder is moved."""
    import sys
    seen = []
    monkeypatch.setattr("subprocess.call", lambda cmd, **k: seen.append(cmd) or 0)
    monkeypatch.setattr(L, "install_exit_notice", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_ui(cli.argparse.Namespace()) == 0
    assert seen[0][:4] == [sys.executable, "-m", "streamlit", "run"]


def test_launchers_survive_the_folder_being_moved():
    """activate.bat + a bare `python` found some OTHER Python after the folder
    was moved; the launchers call the venv's python.exe by its full path."""
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("start_ui.bat", "start_all.bat", "start_listen.bat", "run_queue.bat"):
        text = open(os.path.join(root, name), encoding="ascii").read()
        assert 'call "venv\\Scripts\\activate.bat"' not in text, name
        assert not re.search(r"(?im)^\s*python\s", text), name
        assert not re.search(r'cmd /c "python\b', text), name
        assert '"%PY%"' in text, name
