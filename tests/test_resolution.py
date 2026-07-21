"""Export resolution selector — separate from aspect; sizes, estimate, overrides."""

from shortforge.config import Config
from shortforge.reframe import resolution as R
from shortforge.reframe import parse_aspect


def test_short_side_presets_and_custom():
    assert R.short_side("1080p") == 1080
    assert R.short_side("720p") == 720
    assert R.short_side("480p") == 480
    assert R.short_side("1080x1920") is None                       # custom WxH, not a preset


def test_dims_for_combines_resolution_and_aspect():
    assert R.dims_for("1080p", "9:16") == (1080, 1920)
    assert R.dims_for("720p", "9:16") == (720, 1280)
    assert R.dims_for("480p", "9:16") == (480, 854)                # 480*16/9, evened
    assert R.dims_for("1080p", "16:9") == (1920, 1080)
    assert R.dims_for("720p", "1:1") == (720, 720)
    assert R.dims_for("1080x1350", "9:16") == (1080, 1350)         # custom overrides aspect


def test_apply_resolution_sets_width_height():
    cfg = Config.load()
    cfg.override("reframe.resolution", "720p")
    R.apply_resolution(cfg)
    assert int(cfg.get("reframe.width")) == 720 and int(cfg.get("reframe.height")) == 720
    assert parse_aspect(cfg.get("reframe.aspect", "9:16"), 720, 720) == (720, 1280)


def test_apply_resolution_custom_wxh_routes_to_aspect():
    cfg = Config.load()
    cfg.override("reframe.resolution", "1080x1350")
    R.apply_resolution(cfg)
    assert cfg.get("reframe.aspect") == "1080x1350"


def test_estimate_export_lower_res_smaller_and_faster():
    hi = R.estimate_export("1080p", "9:16", 60, 3)
    lo = R.estimate_export("480p", "9:16", 60, 3)
    assert lo["mb_per_clip"] < hi["mb_per_clip"]
    assert lo["total_mb"] < hi["total_mb"]
    assert "faster" in lo["render_speed"] and hi["render_speed"] == "baseline"


def test_no_dub_override_forces_original_audio(monkeypatch):
    import types
    import cli
    cfg = Config.load()
    cfg.override("localize.dub", True)
    args = types.SimpleNamespace(no_dub=True, resolution="720p", logo_size="0.2")
    cli._apply_common_overrides(cfg, args)
    assert cfg.get("localize.dub") is False                        # forced off
    assert cfg.get("reframe.resolution") == "720p"
    assert str(cfg.get("brand.size")) == "0.2"
