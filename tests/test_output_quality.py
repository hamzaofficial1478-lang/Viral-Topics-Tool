"""Output sharpness: why clips looked pixelated, and what now prevents it.

The operator's report was "the output quality is very low, the videos are
pixelated... the quality gets down when I upload videos on Facebook". Three
separate causes, none of which is an encoder setting on its own:

  1. The download was capped at 1080p. Cropping 1920x1080 to 9:16 leaves a
     608px-wide strip that must then be enlarged 1.78x to reach 1080x1920.
     Detail that was never downloaded cannot be encoded back in.
  2. The finished deliverable was encoded at CRF 20 / preset *veryfast* -- a
     speed compromise, on the one artefact that gets re-encoded again by
     Facebook and so needs headroom.
  3. The tracked path resized with INTER_AREA, which OpenCV documents as
     degenerating to nearest-neighbour when ENLARGING -- which is what this
     pipeline nearly always does.
"""

import pytest

from shortforge.config import Config
from shortforge.reframe import resolution as R
from shortforge.reframe.crop import compute_crop
from shortforge.render import compose
from shortforge.render import render as RD


# --- 1. the root cause: the source was too small ----------------------------- #

def test_a_1080p_source_cannot_fill_a_1080x1920_vertical_clip():
    """The arithmetic behind the complaint, pinned so it can't regress."""
    cw, _ch, _x, _y = compute_crop(1920, 1080, 1080, 1920)
    assert cw == 608                       # all the real width there is
    assert R.upscale_factor(1920, 1080, "1080p", "9:16") == pytest.approx(1.776, abs=0.01)


def test_a_4k_source_fills_it_with_room_to_spare():
    assert R.upscale_factor(3840, 2160, "1080p", "9:16") < 1.0     # downscale = sharp


def test_source_height_needed_is_the_output_height():
    assert R.source_height_needed("1080p", "9:16") == 1920
    assert R.source_height_needed("720p", "9:16") == 1280


def test_download_ceiling_follows_what_the_export_actually_needs():
    """A 720p export must not pull 4K, and a 1080p vertical export must."""
    from shortforge.ingest.ingest import source_ceiling
    cfg = Config.load()
    cfg.override("reframe.aspect", "9:16")

    cfg.override("reframe.resolution", "1080p")
    assert source_ceiling(cfg) == 2160     # needs 1920 tall -> next tier up

    cfg.override("reframe.resolution", "720p")
    assert source_ceiling(cfg) == 1440     # needs 1280 tall -> 1440 is enough


def test_the_1080p_download_cap_is_gone():
    """The single most important line in this change."""
    from shortforge.ingest.ingest import _format_chain
    cfg = Config.load()
    cfg.override("reframe.resolution", "1080p")
    cfg.override("reframe.aspect", "9:16")
    assert "height<=1080" not in _format_chain(cfg)[0]
    assert "height<=2160" in _format_chain(cfg)[0]


def test_max_height_still_prevents_an_8k_download():
    from shortforge.ingest.ingest import source_ceiling
    cfg = Config.load()
    cfg.override("ingest.max_height", 1440)
    cfg.override("reframe.resolution", "2160p")
    assert source_ceiling(cfg) == 1440


def test_an_explicit_format_still_wins():
    from shortforge.ingest.ingest import _format_chain
    cfg = Config.load()
    cfg.override("ingest.format", "bv*[height<=720]+ba")
    assert _format_chain(cfg)[0] == "bv*[height<=720]+ba"


# --- 2. the encode itself ---------------------------------------------------- #

def test_default_quality_beats_the_old_crf20_veryfast():
    cfg = Config.load()
    args = RD.video_encode_args(cfg)
    assert "18" == args[args.index("-crf") + 1]
    assert "medium" == args[args.index("-preset") + 1]


def test_maximum_tier_is_actually_the_best_one():
    cfg = Config.load()
    cfg.override("render.quality", "maximum")
    args = RD.video_encode_args(cfg)
    assert args[args.index("-crf") + 1] == "16"
    assert args[args.index("-preset") + 1] == "slow"


def test_explicit_crf_overrides_the_tier():
    """The fine-grained knob has to keep working as an escape hatch."""
    cfg = Config.load()
    cfg.override("render.quality", "fast")
    cfg.override("render.crf", 14)
    assert RD.video_encode_args(cfg)[RD.video_encode_args(cfg).index("-crf") + 1] == "14"


def test_output_is_tagged_rec709():
    """Untagged H.264 leaves the player -- and Facebook's transcoder -- to
    guess, which is where 'washed out after upload' comes from."""
    for q in ("x264", "qsv", "nvenc", "amf"):
        cfg = Config.load()
        cfg.override("render.encoder", q)
        args = RD.video_encode_args(cfg, encoder="libx264" if q == "x264" else f"h264_{q}")
        assert "bt709" in args, q


def test_hardware_encoders_track_the_quality_tier_too():
    cfg = Config.load()
    cfg.override("render.quality", "maximum")
    a = RD.video_encode_args(cfg, encoder="h264_qsv")
    assert a[a.index("-global_quality") + 1] == "18"


def test_audio_bitrate_raised_for_platform_reencode():
    assert Config.load().get("render.audio_bitrate") == "192k"


# --- 3. scaling quality ------------------------------------------------------ #

def test_scaling_uses_lanczos_not_the_bicubic_default():
    fg = compose.build_filtergraph(1920, 1080, 1080, 1920)
    assert "flags=lanczos" in fg


def test_sharpen_is_applied_when_the_crop_must_be_enlarged():
    fg = compose.build_filtergraph(1920, 1080, 1080, 1920, sharpen=True)
    assert "unsharp=" in fg


def test_sharpen_is_absent_when_not_asked_for():
    assert "unsharp=" not in compose.build_filtergraph(3840, 2160, 1080, 1920)


def test_tracked_path_sharpens_too_even_though_it_is_pre_cropped():
    """OpenCV did the resize upstream, so skipping the filter here would have
    left exactly the clips that need it most unsharpened."""
    fg = compose.build_filtergraph(1920, 1080, 1080, 1920,
                                   pre_cropped=True, sharpen=True)
    assert "unsharp=" in fg
    assert fg.endswith("[v]")


def test_blur_fill_also_gets_the_better_scaler():
    fg = compose.build_filtergraph(1920, 1080, 1080, 1920, fill="blur")
    assert fg.count("flags=lanczos") >= 2


# --- 4. reachable from every surface the operator uses ----------------------- #

def test_the_ui_offers_4k_and_2k():
    keys = [k for k, _ in R.CHOICES]
    assert "2160p" in keys and "1440p" in keys
    assert "4K" in dict(R.CHOICES)["2160p"]


def test_phone_can_ask_for_4k_and_maximum_quality():
    from shortforge.remote_control import parse_settings
    s = parse_settings("https://youtu.be/x 3 clips 4k maximum quality")
    assert s["resolution"] == "2160p"
    assert s["quality"] == "maximum"


def test_quality_no_longer_secretly_means_resolution():
    """"quality" was aliased to RESOLUTION, so "quality=high" set a pixel size."""
    from shortforge.remote_control import parse_settings
    assert parse_settings("quality=maximum")["quality"] == "maximum"


def test_footage_adjectives_do_not_set_the_encoder():
    from shortforge.remote_control import parse_settings
    assert "quality" not in parse_settings("5 fast paced clips")
    assert "quality" not in parse_settings("6 clips of 30s high energy hooks")


def test_quality_reaches_the_queue():
    from shortforge.queue import JOB_SETTINGS
    assert JOB_SETTINGS["quality"] == "render.quality"


def test_the_sample_tool_and_production_use_the_same_scaler():
    """`encode-sample` is what the operator judges quality WITH. If its graph
    scaled differently from the real render, the comparison would measure the
    wrong thing -- the probe/production drift the project rules call out."""
    from shortforge.reframe.crop import _FLAGS
    assert _FLAGS == compose.SCALE_FLAGS
