"""YouTube shows its full format list only to clients that can run its player
JavaScript. yt-dlp needs a runtime for that (Deno; Node/Bun also work) — and
when none is installed, that must be visible, never a silent drop in quality."""

import importlib
import os

import pytest

from shortforge.config import Config

I = importlib.import_module("shortforge.ingest.ingest")


@pytest.fixture
def no_path(monkeypatch, tmp_path):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    return tmp_path


def test_a_runtime_on_path_is_handed_to_ytdlp(monkeypatch, tmp_path):
    import shutil
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/bin/node" if name == "node" else None)
    assert I.find_js_runtime() == ("node", "/usr/bin/node")
    opts = I._base_opts(Config.load(), str(tmp_path))
    assert opts["js_runtimes"] == {"node": {"path": "/usr/bin/node"}}


def test_deno_is_preferred(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    assert I.find_js_runtime() == ("deno", "/bin/deno")


def test_a_fresh_winget_install_is_found_before_path_catches_up(no_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    links = no_path / "Microsoft" / "WinGet" / "Links"
    links.mkdir(parents=True)
    (links / "deno.exe").write_bytes(b"")
    assert I.find_js_runtime() == ("deno", str(links / "deno.exe"))


def test_no_runtime_is_logged_not_silent(no_path, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(I, "_js_warned", False)
    with caplog.at_level("WARNING"):
        opts = I._base_opts(Config.load(), str(tmp_path))
    assert "js_runtimes" not in opts
    assert "Deno" in caplog.text and "update.bat" in caplog.text


def test_doctor_reports_a_missing_runtime(no_path):
    from shortforge import doctor
    c = doctor._youtube_js()
    assert c.status == doctor.WARN
    assert "Deno" in c.detail and "update.bat" in c.fix


def test_quality_failure_points_at_the_missing_runtime(no_path):
    from shortforge.shorts import youtube as YT
    e = YT._quality_error({"width": 1080, "height": 1920}, "the stream was cut off")
    assert "Deno" in str(e) and "update.bat" in str(e)


def test_shorts_page_warns_when_deno_is_missing(no_path, monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    from shortforge.shorts import store as S
    from shortforge.ui import shorts_tab as T
    monkeypatch.setattr(S, "data_dir", lambda cfg=None: str(tmp_path / "data"))
    monkeypatch.setattr(T, "_js_seen", (0.0, False))      # forget an earlier answer
    at = AppTest.from_string(
        "from shortforge.ui import shorts_tab\nshorts_tab.render(run_every=None)",
        default_timeout=30).run()
    assert any("Deno is not installed" in w.value for w in at.warning)


def test_setup_and_update_install_deno_and_the_solver():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("setup.bat", "update.bat"):
        text = open(os.path.join(root, name), encoding="ascii").read()
        assert "DenoLand.Deno" in text, name
        assert '"yt-dlp[default]"' in text, name
    req = open(os.path.join(root, "requirements.txt"), encoding="utf-8").read()
    assert "yt-dlp[default]" in req


def test_update_bat_is_unchanged_up_to_the_pull():
    """Windows keeps reading a running .bat at the same byte offset after git
    replaces it, so nothing before the pull may move between versions."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        old = subprocess.run(["git", "show", "c1727cb:update.bat"], cwd=root,
                             capture_output=True, check=True).stdout.decode()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("git history not available")
    new = open(os.path.join(root, "update.bat"), encoding="ascii").read()
    cut = old.index('git pull origin "!BRANCH!"')
    cut = old.index("\n", cut) + 1
    assert new[:cut] == old[:cut]


def test_ytdlp_version_is_actually_read():
    """yt_dlp has no top-level __version__; reading it always gave "?", so the
    doctor's "yt-dlp is old" warning could never fire."""
    from yt_dlp.version import __version__
    from shortforge import doctor
    assert I.ytdlp_version() == __version__
    assert "?" not in doctor._ytdlp().detail
