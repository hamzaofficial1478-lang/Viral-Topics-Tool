"""Link ingestion reliability: error classification, the cookie fallback chain,
and the friendly bot-detection message."""

import pytest

import importlib

from shortforge.config import Config
from shortforge.utils import ShortForgeError

I = importlib.import_module("shortforge.ingest.ingest")   # the module, not the re-exported fn


def test_classify_buckets():
    assert isinstance(I._classify(Exception("Sign in to confirm you're not a bot")), I._BotWall)
    assert isinstance(I._classify(Exception("Private video. Sign in")), I._Unavailable)
    assert isinstance(I._classify(Exception("This video is not available")), I._Unavailable)
    assert isinstance(I._classify(Exception("The read operation timed out")), I._Transient)
    assert isinstance(I._classify(Exception("Connection reset by peer")), I._Transient)
    plain = I._classify(Exception("some weird extractor bug"))
    assert not isinstance(plain, (I._BotWall, I._Unavailable, I._Transient))


def test_auth_strategies_order():
    cfg = Config.load()
    cfg.override("ingest.cookies", "/tmp/cookies.txt")
    cfg.override("ingest.cookies_from_browser", "firefox")
    labels = [lbl for lbl, _ in I._auth_strategies(cfg)]
    assert labels[0].startswith("cookies.txt")           # configured cookies first
    assert "firefox browser cookies" == labels[1]        # then browser
    assert labels[-1] == "no cookies"                    # no-cookies last


def test_auth_strategies_default_is_just_no_cookies():
    labels = [lbl for lbl, _ in I._auth_strategies(Config.load())]
    assert labels == ["no cookies"]                      # nothing configured


def test_resolve_auth_falls_back_to_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "p.json"))
    from shortforge.providers import store as S
    st = {"providers": [], "credentials": [], "youtube_auth":
          {"cookies_from_browser": "firefox", "cookies_file": None}}
    S.save_store(st)
    cookies_file, browser = I._resolve_auth(Config.load())   # nothing in cfg → store used
    assert browser == "firefox" and cookies_file is None
    # An invalid browser name is ignored (never passed to yt-dlp).
    st["youtube_auth"]["cookies_from_browser"] = "safari"
    S.save_store(st)
    assert I._resolve_auth(Config.load())[1] is None


def _fake_dl_factory(tmp_path, bot_for):
    """A _download_with_retries stub: raises _BotWall for the named strategies,
    succeeds otherwise (returning a fake info + an existing file)."""
    (tmp_path / "downloads").mkdir(exist_ok=True)
    fp = tmp_path / "downloads" / "vid.mp4"
    fp.write_bytes(b"x")

    def fake(url, opts, cfg):
        if "cookiesfrombrowser" in opts and "browser" in bot_for:
            raise I._BotWall("Sign in to confirm you're not a bot")
        if "cookiesfrombrowser" not in opts and "cookiefile" not in opts and "none" in bot_for:
            raise I._BotWall("Sign in to confirm you're not a bot")
        return ({"id": "vid", "title": "My clip", "duration": 12.0}, str(fp))
    return fake


@pytest.fixture
def _stub_probe(monkeypatch):
    monkeypatch.setattr(I, "_require_ytdlp", lambda: object())
    monkeypatch.setattr(I, "content_key", lambda p: "hash")
    monkeypatch.setattr(I, "ffprobe_info",
                        lambda p: type("M", (), {"duration": 12.0, "has_audio": True,
                                                 "width": 1920, "height": 1080})())


def test_fallback_chain_uses_next_strategy_on_bot_wall(tmp_path, monkeypatch, _stub_probe):
    monkeypatch.setattr(I, "_download_with_retries",
                        _fake_dl_factory(tmp_path, bot_for={"browser"}))
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    cfg.override("ingest.cookies_from_browser", "firefox")
    meta = I._ingest_url("https://www.youtube.com/watch?v=vid", cfg)
    assert meta.video_id == "vid" and meta.title == "My clip"   # no-cookies strategy won


def test_all_strategies_bot_wall_raises_friendly_message(tmp_path, monkeypatch, _stub_probe):
    monkeypatch.setattr(I, "_download_with_retries",
                        _fake_dl_factory(tmp_path, bot_for={"browser", "none"}))
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    cfg.override("ingest.cookies_from_browser", "firefox")
    with pytest.raises(ShortForgeError) as ei:
        I._ingest_url("https://www.youtube.com/watch?v=vid", cfg)
    assert "requiring authentication" in str(ei.value)          # friendly, not raw yt-dlp
    assert "Settings" in str(ei.value) and "not a bot" not in str(ei.value).split("(")[0]


def test_unavailable_is_reported_immediately(tmp_path, monkeypatch, _stub_probe):
    def fake(url, opts, cfg):
        raise I._Unavailable("Private video")
    monkeypatch.setattr(I, "_download_with_retries", fake)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    with pytest.raises(ShortForgeError) as ei:
        I._ingest_url("https://www.youtube.com/watch?v=x", cfg)
    assert "unavailable or not a valid video" in str(ei.value)


def test_test_youtube_auth_handles_missing_ytdlp(monkeypatch):
    monkeypatch.setattr(I, "_require_ytdlp",
                        lambda: (_ for _ in ()).throw(ShortForgeError("yt-dlp is not installed.")))
    ok, detail = I.test_youtube_auth("https://youtube.com/watch?v=x", Config.load())
    assert ok is False and "yt-dlp is not installed" in detail
