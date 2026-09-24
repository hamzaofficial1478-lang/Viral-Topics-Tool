"""The 📥 YT Shorts screen, driven headless.

The operator's requirement, in their words: "when user opens the program [and]
opens the YT Shorts tab the saved settings must not be changed once saved" and
"the saved settings must not get deleted". So these tests change something,
throw the whole app away, start a brand-new one and check it is still there.
"""

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from shortforge.shorts import store as S  # noqa: E402

_APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def dd(tmp_path, monkeypatch):
    d = str(tmp_path / "shorts_data")
    monkeypatch.setattr(S, "data_dir", lambda cfg=None: d)
    # never spawn a real downloader from a test
    import shortforge.ui.shorts_tab as T
    monkeypatch.setattr(T, "ensure_worker", lambda dd: "started (test)")
    return d


def _shorts(at=None):
    at = at or AppTest.from_file(_APP, default_timeout=30).run()
    at.sidebar.radio[0].set_value("📥 YT Shorts").run()
    return at


def _button(at, starts: str):
    return next(b for b in at.button if b.label.startswith(starts))


def test_screen_renders_with_no_channels(dd):
    at = _shorts()
    assert not at.exception
    assert any("YouTube Shorts downloader" in h.value for h in at.header)
    assert any("No channels saved yet" in i.value for i in at.info)


def test_adding_channels_needs_the_rights_box(dd):
    at = _shorts()
    at.text_area(key="sh_add_text").set_value("@creatorone\n@creatortwo").run()
    assert _button(at, "➕ Add").disabled                   # box not ticked
    at.checkbox(key="sh_add_rights").check().run()
    assert not _button(at, "➕ Add").disabled


def test_channels_and_counts_survive_a_complete_restart(dd):
    at = _shorts()
    at.text_area(key="sh_add_text").set_value("@creatorone\n@creatortwo").run()
    at.number_input(key="sh_add_count").set_value(7).run()
    at.checkbox(key="sh_add_rights").check().run()
    _button(at, "➕ Add").click().run()
    assert [c["key"] for c in S.load_channels(dd)["channels"]] == ["@creatorone", "@creatortwo"]

    # Change ONE channel's count and switch the other off, in the UI.
    at.number_input(key="sh_cnt_@creatorone").set_value(25).run()
    at.toggle(key="sh_on_@creatortwo").set_value(False).run()

    # A brand-new app — nothing carried over in memory.
    fresh = _shorts()
    assert fresh.number_input(key="sh_cnt_@creatorone").value == 25
    assert fresh.number_input(key="sh_cnt_@creatortwo").value == 7
    assert fresh.toggle(key="sh_on_@creatortwo").value is False
    assert fresh.toggle(key="sh_on_@creatorone").value is True


def test_the_download_button_counts_only_switched_on_channels(dd):
    S.add_channels(dd, "@alpha\n@bravo", 10, rights_confirmed=True)
    S.update_channel(dd, "@bravo", enabled=False)
    at = _shorts()
    go = next(b for b in at.button if b.key == "sh_go")
    assert "10 new Short(s) from 1 channel(s)" in go.label
    go.click().run()
    assert S.load_run(dd)["channels_to_list"] == ["@alpha"]


def test_removing_a_channel_asks_first(dd):
    S.add_channels(dd, "@keepme", 5, rights_confirmed=True)
    at = _shorts()
    next(b for b in at.button if b.key == "sh_rm_@keepme_btn").click().run()
    assert [c["key"] for c in S.load_channels(dd)["channels"]] == ["@keepme"]   # not yet
    next(b for b in at.button if b.key == "sh_rm_yes_@keepme").click().run()
    assert S.load_channels(dd)["channels"] == []


def test_pasted_links_are_queued(dd):
    at = _shorts()
    at.text_area(key="sh_links").set_value(
        "https://www.youtube.com/shorts/abcdefghijk\nnot a link").run()
    at.checkbox(key="sh_links_rights").check().run()
    next(b for b in at.button if b.key == "sh_links_go").click().run()
    assert [i["id"] for i in S.load_run(dd)["items"]] == ["abcdefghijk"]
    assert any("Not a YouTube video link" in w.value for w in at.warning)


def test_settings_are_saved_and_reload(dd):
    at = _shorts()
    q = next(s for s in at.selectbox if s.label == "Quality")
    q.set_value("1080").run()
    save = next(b for b in at.button if b.key == "sh_save_settings")
    save.click().run()
    assert S.settings(dd)["max_height"] == "1080"
    fresh = _shorts()
    q2 = next(s for s in fresh.selectbox if s.label == "Quality")
    assert q2.value == "1080"


def test_history_lists_downloads(dd):
    S.record_download(dd, {"id": "abcdefghijk", "url": "https://youtube.com/shorts/abcdefghijk",
                           "title": "A cat looks at us", "description": "d",
                           "hashtags": ["#cat"], "channel_key": "@a", "channel_name": "A",
                           "width": 1080, "height": 1920, "filesize": 5_000_000,
                           "downloaded_at": 1_700_000_000, "file": "/nowhere.mp4"})
    at = _shorts()
    assert not at.exception
    assert any(m.label == "Downloaded" and str(m.value) == "1" for m in at.metric)
