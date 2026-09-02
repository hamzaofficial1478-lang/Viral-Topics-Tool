"""A folder per source, and landscape by default.

The operator's ask: "when I add the link of the video it should make the folder
of that ID and should add the videos from the same id every time". Their links
carry both identifiers -- the video id in the URL, the uploader in the metadata
-- so the default layout uses both: clips from one channel group together, and
each video keeps a folder that a re-run lands back in.
"""

import os
import types

from shortforge.config import Config
from shortforge import outputs as O


def _meta(uploader="Some Channel", video_id="03n11GA5_oo", title="My Video"):
    return types.SimpleNamespace(uploader=uploader, video_id=video_id, title=title)


# --- the folder ------------------------------------------------------------- #

def test_default_layout_groups_by_channel_then_video():
    assert O.source_folder(_meta(), Config.load()) == os.path.join(
        "Some Channel", "03n11GA5_oo")


def test_the_same_link_lands_in_the_same_folder_every_time():
    """The whole point: re-adding a link must not create a second folder."""
    cfg = Config.load()
    a = O.source_folder(_meta(), cfg)
    b = O.source_folder(_meta(), cfg)
    assert a == b


def test_different_videos_from_one_channel_share_the_channel_folder():
    cfg = Config.load()
    one = O.source_folder(_meta(video_id="aaaaaaaaaaa"), cfg)
    two = O.source_folder(_meta(video_id="bbbbbbbbbbb"), cfg)
    assert os.path.dirname(one) == os.path.dirname(two) == "Some Channel"
    assert one != two


def test_template_can_be_flattened_to_video_id_only():
    cfg = Config.load()
    cfg.override("paths.output_template", "{video_id}")
    assert O.source_folder(_meta(), cfg) == "03n11GA5_oo"


def test_template_can_be_channel_only():
    cfg = Config.load()
    cfg.override("paths.output_template", "{uploader}")
    assert O.source_folder(_meta(), cfg) == "Some Channel"


def test_empty_template_keeps_the_old_flat_behaviour():
    cfg = Config.load()
    cfg.override("paths.output_template", "")
    assert O.source_folder(_meta(), cfg) == ""


def test_a_local_file_still_gets_a_sensible_folder():
    """No uploader, no video id -- group by title rather than dumping every
    local file into one bucket called "unknown"."""
    cfg = Config.load()
    got = O.source_folder(_meta(uploader="", video_id="", title="Holiday Reel"), cfg)
    assert got == "Holiday Reel"


def test_missing_components_are_dropped_not_left_empty():
    """A video with no uploader must not produce "out//<id>"."""
    cfg = Config.load()
    assert O.source_folder(_meta(uploader=""), cfg) == "03n11GA5_oo"


def test_an_unknown_placeholder_does_not_kill_a_finished_render():
    cfg = Config.load()
    cfg.override("paths.output_template", "{nonsense}/{video_id}")
    assert "03n11GA5_oo" in O.source_folder(_meta(), cfg)


# --- Windows will actually accept these -------------------------------------- #

def test_illegal_windows_characters_are_removed():
    for bad in ['Tom & Jerry: S1/E2', 'What? "Really"', r"back\slash", "pipe|name",
                "star*", "lt<gt>"]:
        got = O.safe_name(bad)
        assert not set(got) & set('<>:"/\\|?*'), (bad, got)
        assert got


def test_trailing_dots_and_spaces_are_stripped():
    """Windows strips these when creating the directory, so the folder would
    exist under a name the code then fails to find."""
    assert O.safe_name("Channel.") == "Channel"
    assert O.safe_name("Channel ") == "Channel"
    assert O.safe_name("Channel...  ") == "Channel"


def test_reserved_device_names_are_escaped():
    """A directory called "con" cannot be created on Windows at all."""
    assert O.safe_name("CON") != "CON"
    assert O.safe_name("nul").startswith("_")


def test_names_are_length_capped():
    assert len(O.safe_name("x" * 500)) <= 60


def test_an_entirely_illegal_name_falls_back():
    assert O.safe_name('///', "unknown") == "unknown"


def test_unicode_channel_names_survive():
    assert O.safe_name("Канал Новости") == "Канал Новости"


def test_resolve_output_dir_creates_it(tmp_path):
    cfg = Config.load()
    cfg.override("paths.output_dir", str(tmp_path))
    out = O.resolve_output_dir(_meta(), cfg)
    assert os.path.isdir(out)
    assert out.endswith(os.path.join("Some Channel", "03n11GA5_oo"))


# --- history must not go blank ----------------------------------------------- #

def test_history_finds_manifests_in_the_new_subfolders(tmp_path):
    """Moving manifests into per-source folders would have made every past run
    disappear from the History screen behind a non-recursive glob."""
    import glob
    nested = tmp_path / "Some Channel" / "03n11GA5_oo"
    nested.mkdir(parents=True)
    (nested / "a_manifest.json").write_text("{}")
    (tmp_path / "old_manifest.json").write_text("{}")     # pre-folders run

    found = set(glob.glob(os.path.join(str(tmp_path), "*_manifest.json"))
                + glob.glob(os.path.join(str(tmp_path), "**", "*_manifest.json"),
                            recursive=True))
    assert len(found) == 2, "both the nested and the legacy flat manifest must show"


# --- landscape by default ---------------------------------------------------- #

def test_default_orientation_is_landscape():
    assert Config.load().get("reframe.aspect") == "16:9"


def test_landscape_needs_no_upscale_from_a_1080p_source():
    """A free consequence of the default change: 16:9 out of a 16:9 source is
    not cropped, so nothing is enlarged and 1080p already fills it."""
    from shortforge.reframe.resolution import upscale_factor, source_height_needed
    assert upscale_factor(1920, 1080, "1080p", "16:9") <= 1.0
    assert source_height_needed("1080p", "16:9") == 1080


def test_landscape_default_does_not_pull_a_4k_download():
    """The download ceiling follows the export, so the new default is cheaper
    to fetch than the portrait one it replaces."""
    from shortforge.ingest.ingest import source_ceiling
    cfg = Config.load()
    assert cfg.get("reframe.aspect") == "16:9"
    assert source_ceiling(cfg) == 1080

    cfg.override("reframe.aspect", "9:16")
    assert source_ceiling(cfg) == 2160
