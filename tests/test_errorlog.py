"""The 🐛 Error logs screen: every warning/error lands in one file, tagged with
whether it came from long-video clip making or the Shorts downloader."""

import logging
import threading

import pytest

from shortforge import errorlog as E
from shortforge.utils import log


@pytest.fixture
def wd(tmp_path):
    E.install(str(tmp_path))
    return str(tmp_path)


def test_warnings_and_errors_are_recorded_newest_first_info_is_not(wd):
    log.info("just progress")
    log.warning("first problem")
    log.error("second problem")
    got = E.read(wd)
    assert [e["msg"] for e in got] == ["second problem", "first problem"]
    assert [e["level"] for e in got] == ["ERROR", "WARNING"]
    assert {e["source"] for e in got} == {E.APP}


def test_each_section_tags_its_own_problems(wd):
    with E.source(E.LONG):
        log.error("clip render failed")
    with E.source(E.SHORTS):
        log.warning("shorts: abc failed: bot wall")
    log.warning("from the dashboard")
    assert [(e["source"], e["msg"]) for e in E.read(wd)] == [
        (E.APP, "from the dashboard"), (E.SHORTS, "shorts: abc failed: bot wall"),
        (E.LONG, "clip render failed")]


def test_the_tag_belongs_to_the_thread(wd):
    def shorts_worker():
        with E.source(E.SHORTS):
            log.warning("in the shorts thread")
    with E.source(E.LONG):
        t = threading.Thread(target=shorts_worker)
        t.start()
        t.join()
        log.warning("in the long-video thread")
    by_msg = {e["msg"]: e["source"] for e in E.read(wd)}
    assert by_msg == {"in the shorts thread": E.SHORTS, "in the long-video thread": E.LONG}


def test_installing_twice_does_not_double_every_line(wd):
    E.install(wd)
    log.warning("once")
    assert len(E.read(wd)) == 1


def test_clear_and_size_cap(wd, monkeypatch):
    monkeypatch.setattr(E, "_MAX_BYTES", 2000)
    monkeypatch.setattr(E, "_KEEP_LINES", 5)
    for i in range(60):
        log.warning("problem %d", i)
    got = E.read(wd)
    assert len(got) <= 25 and got[0]["msg"] == "problem 59"
    E.clear(wd)
    assert E.read(wd) == []


def test_the_queue_worker_tags_long_video_failures(tmp_path, monkeypatch):
    from shortforge import queue as Q
    from shortforge import runner
    wd = str(tmp_path)
    E.install(wd)
    q = Q.load_queue(wd)
    Q.add_job(q, "https://youtu.be/broken")
    Q.save_queue(q, wd)

    def boom(*a, **k):
        raise RuntimeError("ffmpeg exploded")
    monkeypatch.setattr(runner, "run_pipeline", boom)
    monkeypatch.setattr("shortforge.selfheal.ask_first", lambda: False)
    monkeypatch.setattr("shortforge.selfheal.report", lambda *a, **k: (False, "failed"))
    runner.drain_queue(lambda: __import__("shortforge.config", fromlist=["Config"]).Config.load(),
                       wd, announce=lambda m: None)
    errors = [e for e in E.read(wd) if e["level"] == "ERROR"]
    assert errors and all(e["source"] == E.LONG for e in errors)
    assert any("ffmpeg exploded" in e["msg"] for e in errors)


def test_cli_workers_install_it(tmp_path, monkeypatch):
    import cli
    from shortforge.config import Config
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: _cfg(tmp_path)))
    cli._install_error_log("shorts")
    log.warning("from the shorts process")
    assert E.read(str(tmp_path))[0]["source"] == E.SHORTS
    E.uninstall()
    cli._install_error_log("doctor")
    assert not [h for h in logging.getLogger("shortforge").handlers if isinstance(h, E.Handler)]


def _cfg(tmp_path):
    from shortforge.config import Config, DEFAULTS
    import copy
    c = Config(copy.deepcopy(DEFAULTS))
    c.override("paths.work_dir", str(tmp_path))
    return c


def test_error_logs_screen_shows_and_filters(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    monkeypatch.chdir(tmp_path)
    E.append(".shortforge", "ERROR", "long video blew up", E.LONG)
    E.append(".shortforge", "WARNING", "a short was slow", E.SHORTS)
    app = str(Path(__file__).resolve().parent.parent / "app.py")
    at = AppTest.from_file(app, default_timeout=30).run()
    at.sidebar.radio[0].set_value("🐛 Error logs").run()
    assert not at.exception
    assert any("long video blew up" in e.value for e in at.error)
    assert any("a short was slow" in w.value for w in at.warning)
    at.radio(key="el_which").set_value(E.SHORTS).run()
    assert not any("long video blew up" in e.value for e in at.error)
    at.button(key="el_clear").click().run()
    assert any("No problems recorded" in s.value for s in at.success)
