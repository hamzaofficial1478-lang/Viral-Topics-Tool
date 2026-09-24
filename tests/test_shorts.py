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

def test_quality_order_is_size_then_fps_then_bitrate():
    assert YT.sort_order({"max_height": "best"}) == ["res", "fps", "vbr", "tbr"]
    # compatible prefers H.264 only AFTER size and frame rate are decided
    assert YT.sort_order({"codec": "compatible"})[:3] == ["res", "fps", "vcodec:h264"]


def test_a_1080p_cap_keeps_vertical_1080x1920():
    """The old cap filtered on height, so "Up to 1080p" turned a vertical
    1080x1920 Short into its 608x1080 version. The cap is the SHORT side now."""
    assert YT.sort_order({"max_height": "1080"})[0] == "res:1080"
    fmts = [{"format_id": "a", "vcodec": "vp9", "width": 1080, "height": 1920, "url": "u"},
            {"format_id": "b", "vcodec": "vp9", "width": 2160, "height": 3840, "url": "u"},
            {"format_id": "c", "vcodec": "vp9", "width": 608, "height": 1080, "url": "u"}]
    assert YT.best_offered(fmts, {"max_height": "1080"})["format_id"] == "a"
    assert YT.best_offered(fmts, {"max_height": "best"})["format_id"] == "b"


def test_best_offered_ignores_audio_and_unusable_formats():
    fmts = [{"format_id": "aud", "vcodec": "none", "acodec": "opus", "url": "u"},
            {"format_id": "sb", "vcodec": "none", "width": 90, "height": 160, "url": "u"},
            {"format_id": "nourl", "vcodec": "vp9", "width": 2160, "height": 3840},
            {"format_id": "18", "vcodec": "avc1", "width": 360, "height": 640, "url": "u"},
            {"format_id": "137", "vcodec": "avc1", "width": 1080, "height": 1920, "url": "u"}]
    b = YT.best_offered(fmts, {})
    assert b["format_id"] == "137" and b["short"] == 1080


def test_strict_selector_can_never_pick_a_smaller_picture():
    sel = YT.strict_selector({"width": 1080, "height": 1920})
    assert sel == "bv*[width>=1080][height>=1920]+ba/b[width>=1080][height>=1920]"
    assert "/b" not in sel.replace("/b[", "")          # no bare "any file" fallback


def test_compatible_codec_never_trades_resolution(tmp_path):
    opts = YT.base_opts(Config.load(), {"codec": "compatible", "container": "mp4"}, str(tmp_path))
    assert opts["format_sort"][:2] == ["res", "fps"]              # resolution decided first
    assert opts["outtmpl"].startswith(str(tmp_path))              # staging, not the output
    assert opts["skip_unavailable_fragments"] is False            # no holes in the video


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


# --- clean output folder + numbering (operator: "only videos", "count 4,5,6") -- #

def test_numbering_continues_across_runs_and_never_goes_back(dd, tmp_path):
    folder = str(tmp_path / "out" / "Chan")
    os.makedirs(folder)
    assert S.next_number(dd, folder) == 1
    for n in (1, 2, 3):
        open(os.path.join(folder, f"{n} - Same title.mp4"), "w").close()
        S.commit_number(dd, folder, n)
    assert S.next_number(dd, folder) == 4                         # next batch: 4, 5, 6
    for n in (1, 2, 3):                                            # operator deletes them
        os.remove(os.path.join(folder, f"{n} - Same title.mp4"))
    assert S.next_number(dd, folder) == 4                         # still 4, not 1
    os.remove(os.path.join(dd, S.COUNTERS_FILE))                   # counter lost…
    open(os.path.join(folder, "9 - x.mp4"), "w").close()
    assert S.next_number(dd, folder) == 10                         # …the folder still says


def test_numbers_are_per_folder(dd, tmp_path):
    a, b = str(tmp_path / "A"), str(tmp_path / "B")
    S.commit_number(dd, a, 5)
    assert S.next_number(dd, a) == 6 and S.next_number(dd, b) == 1


def test_numbered_names_are_windows_safe():
    st = {"name_style": "number_title"}
    assert YT.numbered_name(4, 'Last One Is "Unbelievable" #shorts?', st) == \
        "4 - Last One Is Unbelievable #shorts"
    assert YT.numbered_name(4, "ends with a dot...", st) == "4 - ends with a dot"
    assert YT.numbered_name(4, "anything", {"name_style": "number"}) == "4"
    assert YT.numbered_name(4, "", st) == "4"


def test_extra_files_are_off_by_default():
    s = S.DEFAULT_SETTINGS
    assert not s["sidecar_txt"] and not s["sidecar_json"] and not s["save_thumbnail"]
    assert s["embed_metadata"] is True
    assert "write_thumbnail" not in s        # the old key can't switch it back on


def test_title_description_hashtags_go_inside_the_video(tmp_path):
    """Real ffmpeg: stream copy + tags, readable back with ffprobe."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg not installed")
    src = str(tmp_path / "in.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x568:rate=25:duration=1", "-f", "lavfi", "-i",
                    "sine=duration=1", "-c:v", "libx264", "-c:a", "aac", "-shortest", src],
                   check=True)
    dst = str(tmp_path / "out.mp4")
    ok = YT.embed_metadata(src, dst, {"title": "My title", "description": "Some words",
                                      "hashtags": ["#one", "#two"], "channel_name": "Chan"})
    assert ok
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags",
                          "-of", "json", dst], capture_output=True, text=True).stdout
    tags = {k.lower(): v for k, v in json.loads(out)["format"]["tags"].items()}
    assert tags["title"] == "My title"
    assert "Some words" in tags["description"] and "#one #two" in tags["description"]
    assert tags["comment"] == "#one #two"


def test_tidy_cleans_old_style_folders_and_touches_nothing_else(dd, tmp_path):
    from shortforge.shorts import tidy as TD
    cfg = Config.load()
    root = tmp_path / "out"
    cfg.override("paths.output_dir", str(root))
    folder = root / "shorts" / "Chan"
    folder.mkdir(parents=True)
    ids = ["777Echaaaaa", "fytMfXaaaaa", "P5Ye0Aaaaaa"]
    for i, vid in enumerate(ids):
        base = folder / f"Last One Is Unbelievable #shorts [{vid}]"
        for ext in (".mp4", ".txt", ".json", ".jpg"):
            (base.parent / (base.name + ext)).write_bytes(b"x")
        S.record_download(dd, {"id": vid, "title": "Last One Is Unbelievable #shorts",
                               "downloaded_at": 100 + i, "file": str(base) + ".mp4"})
    (folder / f"Last One Is Unbelievable #shorts [{ids[0]}].f616.mp4.part").write_bytes(b"x" * 10)
    (folder / f"Last One Is Unbelievable #shorts [{ids[1]}].f137.mp4").write_bytes(b"x")
    (folder / "my own notes.txt").write_text("keep me")          # not ours

    p = TD.plan(dd, cfg)
    assert len(p["renames"]) == 3
    assert len(p["deletes"]) == 3 * 3 + 2                         # txt/json/jpg + 2 pieces
    assert not any("my own notes" in d for d, _ in p["deletes"])
    assert sorted(os.listdir(folder)) != []                        # plan changed nothing
    TD.apply(dd, p)
    assert sorted(os.listdir(folder)) == [
        "1 - Last One Is Unbelievable #shorts.mp4",
        "2 - Last One Is Unbelievable #shorts.mp4",
        "3 - Last One Is Unbelievable #shorts.mp4",
        "my own notes.txt"]
    h = S.load_history(dd)["items"]
    assert h[ids[0]]["number"] == 1 and h[ids[0]]["file"].endswith("1 - Last One Is Unbelievable #shorts.mp4")
    out = folder
    assert S.next_number(dd, str(out)) == 4                       # new downloads continue
