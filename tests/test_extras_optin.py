"""STEP 9 — title/description/tags + thumbnail are opt-in (off by default)."""

from shortforge.config import Config


def test_metadata_and_thumbnail_off_by_default():
    # Effective config (DEFAULTS merged with config/settings.yaml) must be OFF, so
    # a plain run makes no metadata LLM call and writes no thumbnail/sidecar.
    c = Config.load()
    assert c.get("metadata.enabled") is False
    assert c.get("thumbnail.enabled") is False


def test_overrides_turn_them_on():
    c = Config.load()
    c.override("metadata.enabled", True)
    c.override("thumbnail.enabled", True)
    assert c.get("metadata.enabled") is True and c.get("thumbnail.enabled") is True
