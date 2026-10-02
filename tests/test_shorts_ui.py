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


_OPEN_TAB = {"label": None}


class _TestTab:
    def __init__(self, tab, is_open):
        self._tab, self.open = tab, is_open

    def __enter__(self):
        return self._tab.__enter__()

    def __exit__(self, *exc):
        return self._tab.__exit__(*exc)


def _tabs_for_tests(labels, *, key):
    """Only the open tab is built — that is what keeps the page fast. A browser
    remembers which tab is open between clicks; Streamlit's test tool can't, so
    tests say which tab they are on (``_shorts(tab=...)``) and it stays open."""
    import streamlit as st
    want = _OPEN_TAB["label"] or labels[0]
    return [_TestTab(t, lab == want) for lab, t in zip(labels, st.tabs(labels))]


@pytest.fixture
def dd(tmp_path, monkeypatch):
    d = str(tmp_path / "shorts_data")
    monkeypatch.setattr(S, "data_dir", lambda cfg=None: d)
    # never spawn a real downloader from a test
    import shortforge.ui.shorts_tab as T
    monkeypatch.setattr(T, "ensure_worker", lambda dd: "started (test)")
    monkeypatch.setattr(T, "lazy_tabs", _tabs_for_tests)
    monkeypatch.setitem(_OPEN_TAB, "label", None)
    return d


def _shorts(at=None, tab: str | None = None):
    """Open the YT Shorts screen, on ``tab`` if given (else 📺 Channels)."""
    _OPEN_TAB["label"] = tab
    at = at or AppTest.from_file(_APP, default_timeout=30).run()
    at.sidebar.radio[0].set_value("📥 YT Shorts").run()
    return at


def _table(at, which="main"):
    """The channel table as a list of row dicts."""
    return next(d for d in at.dataframe if (d.key or "").startswith(f"sh_tbl_{which}_")) \
        .value.to_dict("records")


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

    # Change ONE channel's count and switch the other off, as the table does.
    import shortforge.ui.shorts_tab as T
    keys = ["@creatorone", "@creatortwo"]
    assert T.apply_channel_edits(dd, keys, {0: {"Shorts per run": 25},
                                            1: {"On": False}}) == 2

    # A brand-new app — nothing carried over in memory.
    rows = _table(_shorts())
    assert [(r["Channel"], r["Shorts per run"], r["On"]) for r in rows] == \
        [("@creatorone", 25, True), ("@creatortwo", 7, False)]


def test_table_edits_are_clamped_and_ignore_unknown_rows(dd):
    import shortforge.ui.shorts_tab as T
    S.add_channels(dd, "@alpha", 10, rights_confirmed=True)
    assert T.apply_channel_edits(dd, ["@alpha"], {0: {"Shorts per run": 9999},
                                                  5: {"On": False},
                                                  "x": {"Shorts per run": 3}}) == 1
    assert S.load_channels(dd)["channels"][0]["count"] == 500


def test_the_download_button_counts_only_switched_on_channels(dd):
    S.add_channels(dd, "@alpha\n@bravo", 10, rights_confirmed=True)
    S.update_channel(dd, "@bravo", enabled=False)
    at = _shorts()
    go = next(b for b in at.button if b.key == "sh_go")
    assert "10 new Short(s) from 1 channel(s)" in go.label
    go.click().run()
    assert S.load_run(dd)["channels_to_list"] == ["@alpha"]


def test_removing_a_channel_asks_first(dd):
    S.add_channels(dd, "@keepme\n@dropme", 5, rights_confirmed=True)
    at = _shorts()
    at.multiselect(key="sh_pick_main").select("@dropme (@dropme)").run()
    next(b for b in at.button if b.key == "sh_rm_main").click().run()
    assert len(S.load_channels(dd)["channels"]) == 2                       # not yet
    next(b for b in at.button if b.key == "sh_rm_yes_main").click().run()
    assert [c["key"] for c in S.load_channels(dd)["channels"]] == ["@keepme"]
    assert [r["Channel"] for r in _table(at)] == ["@keepme"]


def test_wishlist_channels_can_be_moved_to_your_channels(dd):
    S.add_channels(dd, "@mineone", 3, rights_confirmed=True)
    S.add_channels(dd, "@wishone\n@wishtwo", 4, rights_confirmed=True, list_name=S.WISHLIST)
    at = _shorts()
    assert [r["Channel"] for r in _table(at, "wishlist")] == ["@wishone", "@wishtwo"]
    at.multiselect(key="sh_pick_wishlist").select("@wishtwo (@wishtwo)").run()
    next(b for b in at.button if b.key == "sh_promote_wishlist").click().run()
    assert not at.exception
    assert [c["key"] for c in S.channels_in(dd, S.MAIN)] == ["@mineone", "@wishtwo"]
    assert [r["Channel"] for r in _table(at)] == ["@mineone", "@wishtwo"]
    assert [r["Channel"] for r in _table(at, "wishlist")] == ["@wishone"]


def test_pasted_links_are_queued(dd):
    at = _shorts(tab="🔗 Paste links")
    at.text_area(key="sh_links").set_value(
        "https://www.youtube.com/shorts/abcdefghijk\nnot a link").run()
    at.checkbox(key="sh_links_rights").check().run()
    next(b for b in at.button if b.key == "sh_links_go").click().run()
    assert [i["id"] for i in S.load_run(dd)["items"]] == ["abcdefghijk"]
    assert any("Not a YouTube video link" in w.value for w in at.warning)


def test_settings_are_saved_and_reload(dd):
    at = _shorts(tab="⚙️ Settings")
    q = next(s for s in at.selectbox if s.label == "Quality")
    q.set_value("1080").run()
    save = next(b for b in at.button if b.key == "sh_save_settings")
    save.click().run()
    assert S.settings(dd)["max_height"] == "1080"
    fresh = _shorts(tab="⚙️ Settings")
    q2 = next(s for s in fresh.selectbox if s.label == "Quality")
    assert q2.value == "1080"


def test_history_lists_downloads(dd):
    S.record_download(dd, {"id": "abcdefghijk", "url": "https://youtube.com/shorts/abcdefghijk",
                           "title": "A cat looks at us", "description": "d",
                           "hashtags": ["#cat"], "channel_key": "@a", "channel_name": "A",
                           "width": 1080, "height": 1920, "filesize": 5_000_000,
                           "downloaded_at": 1_700_000_000, "file": "/nowhere.mp4"})
    at = _shorts(tab="🕘 History")
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
    return _shorts(at, tab="🔎 Find channels")


def test_find_tab_starts_a_background_search(dd, monkeypatch):
    import shortforge.ui.shorts_tab as T
    started = []
    monkeypatch.setattr(T, "spawn_search", lambda d, job_id: started.append(job_id))
    at = _find_tab()
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
    at = _find_tab()
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
    at = _find_tab()
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


def test_only_the_open_tab_is_built(dd, monkeypatch):
    """Every tab used to run on every click — all channel rows, the whole
    download history, past searches — which is what made the page drag."""
    import shortforge.ui.shorts_tab as T
    from shortforge.ui.lazy import lazy_tabs
    monkeypatch.setattr(T, "lazy_tabs", lazy_tabs)
    built = []
    for name in ("_tab_channels", "_tab_find", "_tab_links", "_tab_history", "_tab_settings"):
        real = getattr(T, name)
        monkeypatch.setattr(T, name, lambda *a, _n=name, _r=real, **k: (built.append(_n), _r(*a, **k)))
    at = _shorts()
    assert not at.exception and built == ["_tab_channels"]
    built.clear()
    at.session_state["sh_tab"] = "🕘 History"
    at.run()
    assert not at.exception and built == ["_tab_history"]


def test_history_is_read_once_per_change(dd, monkeypatch):
    S.record_download(dd, {"id": "abcdefghijk", "channel_key": "@a"})
    reads = []
    real = S.load_history
    monkeypatch.setattr(S, "load_history", lambda d: (reads.append(1), real(d))[1])
    S._history_cache.clear()
    assert len(S.history_items(dd)) == 1 and len(S.history_items(dd)) == 1
    assert len(reads) == 1
    S.record_download(dd, {"id": "bbbbbbbbbbb", "channel_key": "@a"})   # file changes
    assert len(S.history_items(dd)) == 2 and len(reads) == 2
    assert S.known_ids(dd) == {"abcdefghijk", "bbbbbbbbbbb"}           # no-repeat: fresh read
