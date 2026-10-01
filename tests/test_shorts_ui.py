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


# --- 🔎 Find channels + wishlist --------------------------------------------- #

def _finished_search(dd):
    """A completed search job on disk, as the background process leaves it."""
    from shortforge.shorts import discover as D
    job = D.new_job(dd, {"mode": "describe", "prompt": "pakistani cooking", "count": 3,
                         "country": "PK", "language": "ur", "use_ai": False})
    job.update(status="done", stage="found 2 of 3",
               effective={"country": "PK", "language": "ur", "country_name": "Pakistan",
                          "language_name": "Urdu"},
               notes=["only 2 of 3 channels fit — 12 were checked"],
               results=[{"channel_id": "UCpk1aaaaaaaaaaaaaaaaaaa", "key": "@lahorekitchen",
                         "url": "https://www.youtube.com/@lahorekitchen", "name": "Lahore Kitchen",
                         "subscribers": 500000, "country": "Pakistan", "country_code": "PK",
                         "country_status": "match", "language": "hi-Latn",
                         "language_label": "Hindi/Urdu (Roman script)", "fit": None,
                         "why": "matches: cooking", "sample_titles": ["biryani kaise banaye"],
                         "avatar": ""},
                        {"channel_id": "UCpk2aaaaaaaaaaaaaaaaaaa", "key": "@karachicooks",
                         "url": "https://www.youtube.com/@karachicooks", "name": "Karachi Cooks",
                         "subscribers": 90000, "country": "Pakistan", "country_code": "PK",
                         "country_status": "match", "language": "ur", "language_label": "Urdu",
                         "fit": None, "why": "", "sample_titles": [], "avatar": ""}])
    D.save_job(dd, job)
    return job


def _find_tab(at=None):
    at = _shorts(at)
    return at


def test_find_tab_starts_a_background_search(dd, monkeypatch):
    import shortforge.ui.shorts_tab as T
    started = []
    monkeypatch.setattr(T, "spawn_search", lambda d, job_id: started.append(job_id))
    at = _shorts()
    go = next(b for b in at.button if b.key == "dc_go")
    assert go.disabled                                        # nothing described yet
    at.text_area(key="dc_prompt").set_value("pakistani cooking in urdu").run()
    at.number_input(key="dc_count").set_value(7).run()
    next(b for b in at.button if b.key == "dc_go").click().run()
    assert len(started) == 1
    from shortforge.shorts import discover as D
    job = D.load_job(dd, started[0])
    assert job["params"]["prompt"] == "pakistani cooking in urdu"
    assert job["params"]["count"] == 7


def test_results_can_be_added_to_the_wishlist(dd):
    _finished_search(dd)
    at = _shorts()
    assert not at.exception
    assert any("2 of 3 found" in m.value for m in at.markdown)
    assert any("only 2 of 3" in i.value for i in at.info)       # the shortfall is explained
    add = next(b for b in at.button if b.key == "dc_add")
    assert add.disabled                                          # nothing ticked, no rights
    next(b for b in at.button if b.label == "Select all").click().run()
    at.checkbox(key="dc_rights").check().run()
    next(b for b in at.button if b.key == "dc_add").click().run()
    wish = S.channels_in(dd, S.WISHLIST)
    assert sorted(c["key"] for c in wish) == ["@karachicooks", "@lahorekitchen"]
    assert S.channels_in(dd, S.MAIN) == []


def test_hidden_channels_are_remembered(dd):
    from shortforge.shorts import discover as D
    _finished_search(dd)
    at = _shorts()
    at.checkbox(key=next(c.key for c in at.checkbox
                         if c.key and c.key.endswith("UCpk2aaaaaaaaaaaaaaaaaaa"))).check().run()
    next(b for b in at.button if b.key == "dc_dismiss").click().run()
    assert "UCpk2aaaaaaaaaaaaaaaaaaa" in D.dismissed(dd)


def test_channels_tab_shows_the_wishlist_and_the_download_switch(dd):
    S.add_channels(dd, "@mineone", 3, rights_confirmed=True)
    S.add_channels(dd, "@wishone", 4, rights_confirmed=True, list_name=S.WISHLIST)
    at = _shorts()
    go = next(b for b in at.button if b.key == "sh_go")
    assert "from 1 channel(s)" in go.label                      # wishlist off by default
    at.checkbox(key="sh_inc_wish").check().run()
    go = next(b for b in at.button if b.key == "sh_go")
    assert "7 new Short(s) from 2 channel(s)" in go.label
    assert S.settings(dd)["include_wishlist"] is True            # remembered
    fresh = _shorts()
    assert fresh.checkbox(key="sh_inc_wish").value is True
