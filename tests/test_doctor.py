"""C1: doctor preflight checks."""

import pytest

from shortforge.config import Config
from shortforge.doctor import run_checks, format_report, preflight, OK, FAIL, _ffmpeg
from shortforge.utils import ShortForgeError


def test_run_checks_returns_all():
    checks = run_checks(Config.load())
    names = {c.name for c in checks}
    assert "ffmpeg + ffprobe" in names
    assert "translation backend" in names
    assert "hardware" in names
    for c in checks:
        assert c.status in (OK, "warn", FAIL)


def test_format_report_mentions_counts():
    report = format_report(run_checks(Config.load()))
    assert "ShortForge doctor" in report
    assert "issue(s)" in report


def test_ffmpeg_check_reflects_path(monkeypatch):
    import shortforge.doctor as d
    monkeypatch.setattr(d.shutil, "which", lambda _n: None)
    assert _ffmpeg().status == FAIL


def test_preflight_raises_when_ffmpeg_missing(monkeypatch):
    import shortforge.doctor as d
    monkeypatch.setattr(d.shutil, "which", lambda _n: None)
    with pytest.raises(ShortForgeError) as e:
        preflight(Config.load())
    assert "ffmpeg" in str(e.value).lower()
