"""YouTube Shorts downloader: parsing, persistence, no-repeat, resume, pause,
bot-wall handling, metadata, and phone commands.

YouTube itself is not contacted here: `list_new_shorts` and `download_short`
are replaced with fakes so the worker's own logic is what is under test.
"""

import json
import os

import pytest

from shortforge.config import Config
from shortforge.shorts import commands as C
from shortforge.shorts import store as S
from shortforge.shorts import worker as W
from shortforge.shorts import youtube as YT
from shortforge.utils import ShortForgeError


@pytest.fixture
def dd(tmp_path):
    return str(tmp_path / "shorts_data")


# --- parsing what the operator pastes ---------------------------------------- #

@pytest.mark.parametrize("text,key,url", [
    ("@MrBeast", "@mrbeast", "https://www.youtube.com/@MrBeast"),
    ("https://www.youtube.com/@MrBeast/shorts", "@mrbeast", "https://www.youtube.com/@MrBeast"),
    ("youtube.com/@MrBeast?si=abc", "@mrbeast", "https://www.youtube.com/@MrBeast"),
    ("https://m.youtube.com/@MrBeast/videos", "@mrbeast", "https://www.youtube.com/@MrBeast"),
    ("UCBR8-60-B28hp2BmDPdntcQ", "channel/UCBR8-60-B28hp2BmDPdntcQ",
     "https://www.youtube.com/channel/UCBR8-60-B28hp2BmDPdntcQ"),
    ("https://www.youtube.com/channel/UCBR8-60-B28hp2BmDPdntcQ/featured",
     "channel/UCBR8-60-B28hp2BmDPdntcQ",
     "https://www.youtube.com/channel/UCBR8-60-B28hp2BmDPdntcQ"),
    ("https://www.youtube.com/c/Google", "c/google", "https://www.youtube.com/c/Google"),
    ("https://www.youtube.com/user/Google", "user/google", "https://www.youtube.com/user/Google"),
])
def test_channel_references_are_normalised(text, key, url):
    assert S.parse_channel(text) == (key, url)


def test_a_video_link_is_not_mistaken_for_a_channel():
    with pytest.raises(ValueError, match="Paste links"):
        S.parse_channel("https://www.youtube.com/shorts/abcdefghijk")


@pytest.mark.parametrize("junk", ["hello", "https://example.com/@someone", "", "@x"])
def test_junk_is_rejected_with_a_reason(junk):
    with pytest.raises(ValueError):
        S.parse_channel(junk)


@pytest.mark.parametrize("link,vid", [
    ("https://www.youtube.com/shorts/abcdefghijk", "abcdefghijk"),
    ("https://youtube.com/shorts/abcdefghijk?feature=share", "abcdefghijk"),
    ("https://youtu.be/abcdefghijk", "abcdefghijk"),
    ("https://www.youtube.com/watch?v=abcdefghijk&t=3", "abcdefghijk"),
    ("https://www.youtube.com/watch?list=x&v=abcdefghijk", "abcdefghijk"),
    ("abcdefghijk", "abcdefghijk"),
    ("https://www.youtube.com/@someone", None),
    ("not a link", None),
])
def test_video_ids_from_links(link, vid):
    assert S.video_id_from_link(link) == vid


# --- saved channels ----------------------------------------------------------- #

def test_adding_channels_requires_rights_confirmation(dd):
    added, problems = S.add_channels(dd, "@creatorone", 5, rights_confirmed=False)
    assert added == [] and "rights" in problems[0]
    assert S.load_channels(dd)["channels"] == []


def test_duplicates_keep_their_own_count(dd):
    S.add_channels(dd, "@creatorone", 5, rights_confirmed=True)
    S.update_channel(dd, "@creatorone", count=42)
    added, problems = S.add_channels(dd, "https://www.youtube.com/@CreatorOne/shorts", 9,
                                     rights_confirmed=True)
    assert added == [] and "already saved" in problems[0]
    assert S.load_channels(dd)["channels"][0]["count"] == 42


def test_many_channels_one_paste(dd):
    added, problems = S.add_channels(dd, "@alpha, @bravo\n@charlie   hello", 3,
                                     rights_confirmed=True)
    assert [c["key"] for c in added] == ["@alpha", "@bravo", "@charlie"]
    assert len(problems) == 1 and problems[0].startswith("hello")


def test_count_is_bounded(dd):
    S.add_channels(dd, "@creatorone", 99999, rights_confirmed=True)
    assert S.load_channels(dd)["channels"][0]["count"] == 500
    S.update_channel(dd, "@creatorone", count=0)
    assert S.load_channels(dd)["channels"][0]["count"] == 1


def test_a_damaged_channels_file_falls_back_to_its_backup(dd):
    """Losing the saved channels is the one thing the operator said must not
    happen. A half-written or corrupted file is read from the .bak."""
    S.add_channels(dd, "@alpha", 5, rights_confirmed=True)
    S.add_channels(dd, "@bravo", 5, rights_confirmed=True)       # .bak now holds @alpha
    with open(os.path.join(dd, S.CHANNELS_FILE), "w") as f:
        f.write("{ this is not json")
    keys = [c["key"] for c in S.load_channels(dd)["channels"]]
    assert keys and "@alpha" in keys


def test_settings_default_and_persist(dd):
    assert S.settings(dd)["max_height"] == "best"               # no compromise by default
    S.update_settings(dd, max_height="1080", not_a_setting=1)
    s = S.settings(dd)
    assert s["max_height"] == "1080" and "not_a_setting" not in s


# --- history: never the same Short twice -------------------------------------- #

def test_history_and_files_on_disk_both_count_as_downloaded(dd, tmp_path):
    S.record_download(dd, {"id": "aaaaaaaaaaa", "channel_key": "@a"})
    out = tmp_path / "out" / "Some Channel"
    out.mkdir(parents=True)
    (out / "A title [bbbbbbbbbbb].mp4").write_bytes(b"x")
    (out / "Half done [ccccccccccc].mp4.part").write_bytes(b"x")      # not finished
    ids = S.known_ids(dd, str(tmp_path / "out"))
    assert {"aaaaaaaaaaa", "bbbbbbbbbbb"} <= ids
    assert "ccccccccccc" not in ids                              # resumed, not skipped


def test_forget_allows_a_redownload(dd):
    S.record_download(dd, {"id": "aaaaaaaaaaa"})
    assert S.forget(dd, "aaaaaaaaaaa") is True
    assert "aaaaaaaaaaa" not in S.known_ids(dd)


# --- the run plan ------------------------------------------------------------- #

def test_adding_to_a_running_run_never_loses_what_is_queued(dd):
    S.start_run(dd, ["@alpha"], ["https://youtu.be/aaaaaaaaaaa"])
    q = S.start_run(dd, ["@bravo", "@alpha"], ["https://youtu.be/aaaaaaaaaaa",
                                                "https://youtu.be/bbbbbbbbbbb", "junk"])
    run = S.load_run(dd)
    assert run["channels_to_list"] == ["@alpha", "@bravo"]
    assert [i["id"] for i in run["items"]] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert q == {"channels": 1, "links": 1, "bad_links": ["junk"]}


def test_power_cut_puts_the_interrupted_short_back(dd):
    S.start_run(dd, [], ["https://youtu.be/aaaaaaaaaaa"])
    S.mutate_run(dd, lambda r: r["items"][0].update(status=S.DOWNLOADING))
    assert S.requeue_interrupted(dd) == 1
    assert S.load_run(dd)["items"][0]["status"] == S.PENDING


def test_retry_only_bot_wall_failures(dd):
    S.start_run(dd, [], ["https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb"])

    def fail(r):
        r["items"][0].update(status=S.FAILED, error='YouTube is requiring authentication ("not a bot")')
        r["items"][1].update(status=S.FAILED, error="This video is unavailable")
    S.mutate_run(dd, fail)
    assert S.retry_failed(dd, only_bot_wall=True) == 1
    st = [i["status"] for i in S.load_run(dd)["items"]]
    assert st == [S.PENDING, S.FAILED]


def test_lock_from_a_dead_process_is_reclaimed(dd):
    os.makedirs(dd, exist_ok=True)
    with open(os.path.join(dd, S.LOCK_FILE), "w") as f:
        f.write("999999")                                        # no such process
    assert S.acquire_lock(dd) is True
    assert S.acquire_lock(dd) is False                           # ours now, and live
    S.release_lock(dd)


# --- metadata: every Short has a title, description and hashtags --------------- #

def test_uploader_hashtags_come_first_then_tags():
    tags, src = YT.hashtags_for({"title": "Big news #Launch", "description": "more #launch #tech",
                                 "tags": ["gadgets", "new phone"]})
    assert tags[:2] == ["#Launch", "#tech"]                       # case-insensitive dedupe
    assert "#newphone" in tags and src == "uploader"


def test_hashtags_are_generated_only_when_there_are_none_and_say_so():
    tags, src = YT.hashtags_for({"title": "Building a treehouse with my brother"},
                                "Wood Works")
    assert src == "generated"
    assert "#shorts" in tags and "#WoodWorks" in tags and "#Building" in tags


def test_empty_description_is_filled_and_flagged():
    assert YT.description_for({"description": "  real words "}) == ("real words", "uploader")
    assert YT.description_for({"title": "A title", "description": ""}) == ("A title", "generated")


def test_sidecar_says_when_shortforge_wrote_the_text(tmp_path):
    vid = tmp_path / "t [aaaaaaaaaaa].mp4"
    vid.write_bytes(b"x")
    rec = {"title": "T", "description": "T", "description_source": "generated",
           "hashtags": ["#a"], "hashtags_source": "generated", "url": "u"}
    txt, js = YT.write_sidecars(str(vid), rec)
    body = open(txt, encoding="utf-8").read()
    assert body.startswith("TITLE\nT\n\nDESCRIPTION\nT\n\nHASHTAGS\n#a")
    assert "written by ShortForge" in body
    assert json.load(open(js, encoding="utf-8"))["title"] == "T"


# --- quality ------------------------------------------------------------------ #

def test_best_quality_has_no_height_cap():
    assert YT.format_chain({"max_height": "best"}) == ["bv*+ba/b", "b"]
    assert YT.format_chain({"max_height": "1080"})[0] == "bv*[height<=1080]+ba/b[height<=1080]"


def test_compatible_codec_never_trades_resolution(tmp_path):
    opts = YT.base_opts(Config.load(), {"codec": "compatible", "container": "mp4"}, str(tmp_path))
    assert opts["format_sort"][:2] == ["res", "fps"]              # resolution decided first
    assert "[%(id)s]" in opts["outtmpl"]                          # on-disk no-repeat key
    assert "format_sort" not in YT.base_opts(Config.load(), {"codec": "best"}, str(tmp_path))


def test_empty_media_file_rotates_the_player_client():
    """Seen live on a Short: the default+tv clients got an empty file. That is a
    client refusal (like a 403), so the next client must be tried."""
    import importlib
    I = importlib.import_module("shortforge.ingest.ingest")
    assert isinstance(I._classify(Exception("ERROR: The downloaded file is empty")), I._Forbidden)


# --- the worker --------------------------------------------------------------- #

@pytest.fixture
def fake_youtube(monkeypatch, tmp_path):
    """list_new_shorts returns the channel's newest ids minus `skip`;
    download_short 'downloads' by writing a file and returning a record."""
    catalogue = {"@alpha": [f"alpha{i:06d}" for i in range(30)]}
    calls = {"downloads": [], "fail": set(), "botwall": set()}

    def fake_list(url, cfg, want, skip, **k):
        key = "@" + url.rsplit("@", 1)[1].lower()
        new = [{"id": i, "title": f"t {i}"} for i in catalogue[key] if i not in skip][:want]
        return {"name": key.lstrip("@").title()}, new, len(new) < want

    def fake_download(item, cfg, st, hook=None):
        if item["id"] in calls["botwall"]:
            raise ShortForgeError('YouTube is requiring authentication ("not a bot")')
        if item["id"] in calls["fail"]:
            raise ShortForgeError("This video is unavailable")
        calls["downloads"].append(item["id"])
        return {"id": item["id"], "title": item.get("title") or item["id"],
                "channel_key": item.get("channel_key"), "file": f"/x/{item['id']}.mp4",
                "filesize": 1000}

    monkeypatch.setattr(YT, "list_new_shorts", fake_list)
    monkeypatch.setattr(YT, "download_short", fake_download)
    return calls


def _cfg(tmp_path):
    cfg = Config.load()
    cfg.override("paths.output_dir", str(tmp_path / "out"))
    return cfg


def test_a_run_takes_each_channels_own_count_and_never_repeats(dd, tmp_path, fake_youtube):
    S.add_channels(dd, "@alpha", 5, rights_confirmed=True)
    S.update_settings(dd, delay_seconds=0)
    msgs = []

    S.start_run(dd, ["@alpha"], [])
    s = W.drain(_cfg(tmp_path), dd=dd, announce=msgs.append)
    assert s["downloaded"] == 5 and s["finished"]
    first = list(fake_youtube["downloads"])
    assert first == [f"alpha{i:06d}" for i in range(5)]           # newest first

    S.start_run(dd, ["@alpha"], [])                               # "download again"
    W.drain(_cfg(tmp_path), dd=dd, announce=msgs.append)
    second = fake_youtube["downloads"][5:]
    assert second == [f"alpha{i:06d}" for i in range(5, 10)]     # the NEXT five
    assert not set(first) & set(second)                           # never the same one
    assert S.load_channels(dd)["channels"][0]["name"] == "Alpha"  # name learned
    assert any("Alpha" in m and "5 Short(s) downloaded" in m for m in msgs)


def test_a_channel_that_runs_out_says_so(dd, tmp_path, fake_youtube):
    S.add_channels(dd, "@alpha", 50, rights_confirmed=True)
    S.update_settings(dd, delay_seconds=0)
    S.start_run(dd, ["@alpha"], [])
    msgs = []
    s = W.drain(_cfg(tmp_path), dd=dd, announce=msgs.append)
    assert s["downloaded"] == 30
    assert "only 30 new Short(s) left" in S.load_run(dd)["notes"]["@alpha"]
    assert any("only 30 new" in m for m in msgs)


def test_repeated_bot_walls_pause_the_run_and_keep_the_work(dd, tmp_path, fake_youtube):
    S.add_channels(dd, "@alpha", 6, rights_confirmed=True)
    S.update_settings(dd, delay_seconds=0, stop_on_bot_wall=3)
    fake_youtube["botwall"] |= {f"alpha{i:06d}" for i in range(6)}
    S.start_run(dd, ["@alpha"], [])
    msgs = []
    s = W.drain(_cfg(tmp_path), dd=dd, announce=msgs.append)
    run = S.load_run(dd)
    assert s["paused"] and run["paused"]
    assert S.counts(run)[S.PENDING] == 6                          # all back in line
    assert any("asking to sign in" in m for m in msgs)

    fake_youtube["botwall"].clear()                               # cookies fixed
    S.set_paused(dd, False)
    s = W.drain(_cfg(tmp_path), dd=dd, announce=lambda m: None)
    assert s["downloaded"] == 6


def test_one_bad_short_does_not_stop_the_rest(dd, tmp_path, fake_youtube):
    S.update_settings(dd, delay_seconds=0)
    fake_youtube["fail"].add("bbbbbbbbbbb")
    S.start_run(dd, [], ["https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb",
                         "https://youtu.be/ccccccccccc"])
    s = W.drain(_cfg(tmp_path), dd=dd, announce=lambda m: None)
    assert (s["downloaded"], s["failed"]) == (2, 1)


def test_a_paused_run_does_nothing(dd, tmp_path, fake_youtube):
    S.start_run(dd, [], ["https://youtu.be/aaaaaaaaaaa"])
    S.set_paused(dd, True)
    assert W.drain(_cfg(tmp_path), dd=dd, announce=lambda m: None)["downloaded"] == 0
    assert fake_youtube["downloads"] == []


def test_never_two_downloaders_at_once(dd, tmp_path, fake_youtube):
    S.start_run(dd, [], ["https://youtu.be/aaaaaaaaaaa"])
    assert S.acquire_lock(dd)
    assert W.drain(_cfg(tmp_path), dd=dd, announce=lambda m: None) == {"locked": True}
    S.release_lock(dd)


def test_should_stop_is_honoured_between_shorts(dd, tmp_path, fake_youtube):
    S.update_settings(dd, delay_seconds=0)
    S.start_run(dd, [], [f"https://youtu.be/{c * 11}" for c in "abc"])
    s = W.drain(_cfg(tmp_path), dd=dd, announce=lambda m: None,
                should_stop=lambda: len(fake_youtube["downloads"]) >= 1)
    assert s["downloaded"] == 1
    assert S.counts(S.load_run(dd))[S.PENDING] == 2               # the rest are kept


# --- phone commands ------------------------------------------------------------ #

def test_shorts_commands_never_capture_clip_queue_phrasing():
    assert C.matches("shorts start") and C.matches("/shorts") and C.matches("Shorts pause")
    assert not C.matches("3 shorts 1:30")                          # clip settings
    assert not C.matches("short clips please")
    assert not C.matches("start")                                  # bare start = clip queue


def test_shorts_start_queues_switched_on_channels(dd):
    S.add_channels(dd, "@alpha\n@bravo", 4, rights_confirmed=True)
    S.update_channel(dd, "@bravo", enabled=False)
    reply = C.handle("shorts start", dd)
    assert "up to 4 new Short(s) from 1 channel(s)" in reply
    assert S.load_run(dd)["channels_to_list"] == ["@alpha"]
    assert "paused" in C.handle("shorts pause", dd).lower()
    assert S.load_run(dd)["paused"] is True
    assert "resumed" in C.handle("shorts start", dd).lower()        # carries on, no re-queue
    assert S.load_run(dd)["channels_to_list"] == ["@alpha"]


def test_shorts_start_with_no_channels_explains(dd):
    assert "No channels are switched on" in C.handle("shorts start", dd)


def test_remote_control_routes_shorts_but_not_links(monkeypatch, tmp_path, dd):
    from shortforge import remote_control as RC
    monkeypatch.setattr(S, "data_dir", lambda cfg=None: dd)
    assert "Shorts" in RC.handle_text("shorts", str(tmp_path))
    # a link with clip settings still goes to the clip queue
    reply = RC.handle_text("https://youtu.be/abcdefghijk 3 shorts 1:30", str(tmp_path))
    assert "Shorts downloader" not in reply
