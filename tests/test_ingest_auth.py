"""Link ingestion reliability: error classification, the cookie fallback chain,
and the friendly bot-detection message."""

import os

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


# --- pasted cookie text + format fallback ----------------------------------- #

def test_pasted_cookie_text_is_written_to_a_file(tmp_path, monkeypatch):
    """Operators paste the file CONTENTS into the cookies field; yt-dlp then got
    '[Errno 22] Invalid argument' because it expects a path."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    pasted = (".youtube.com\tTRUE\t/\tFALSE\t1819444429\tSID\tg.a000AwmDkSdW\n"
              ".youtube.com\tTRUE\t/\tTRUE\t1801591123\t__Secure-ROLLOUT_TOKEN\tCKDex\n")
    path = I._materialise_cookies(pasted)
    assert path and os.path.isfile(path)
    body = open(path, encoding="utf-8").read()
    assert body.startswith("# Netscape")          # header added so yt-dlp accepts it
    assert "SID" in body


def test_real_path_is_passed_through_untouched(tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    assert I._materialise_cookies(str(f)) == str(f)
    assert I._materialise_cookies(None) is None


def test_format_error_is_not_an_auth_error():
    """'Requested format is not available' means we signed in FINE — it must not
    be reported as a bot wall."""
    c = I._classify(Exception("ERROR: [youtube] abc: Requested format is not available"))
    assert isinstance(c, I._NoFormat)
    assert not isinstance(c, I._BotWall)


def test_format_chain_gets_looser():
    from shortforge.config import Config
    chain = I._format_chain(Config.load())
    assert len(chain) >= 3
    assert chain[-1] == "best"                     # always ends with "anything that plays"
    assert len(set(chain)) == len(chain)           # no duplicates


def test_with_format_fallback_loosens_then_succeeds():
    cfg = Config.load()
    tried: list[str] = []

    def attempt(fmt):
        tried.append(fmt)
        if len(tried) < 3:
            raise I._NoFormat("Requested format is not available")
        return "downloaded"

    result, fmt, fell_back = I._with_format_fallback(cfg, attempt)
    assert result == "downloaded" and fell_back is True
    assert fmt == tried[-1] and tried == I._format_chain(cfg)[:3]


def test_with_format_fallback_reports_no_fallback_on_first_hit():
    cfg = Config.load()
    result, fmt, fell_back = I._with_format_fallback(cfg, lambda f: f)
    assert fell_back is False and fmt == I._format_chain(cfg)[0] and result == fmt


def test_with_format_fallback_reraises_when_every_format_refused():
    cfg = Config.load()
    calls = []

    def attempt(fmt):
        calls.append(fmt)
        raise I._NoFormat("Requested format is not available")

    with pytest.raises(I._NoFormat):
        I._with_format_fallback(cfg, attempt)
    assert calls == I._format_chain(cfg)           # exhausted the whole chain


def test_with_format_fallback_does_not_swallow_other_errors():
    """A bot wall must reach the auth chain immediately — retrying formats there
    would just re-hit the wall three more times."""
    def attempt(fmt):
        raise I._BotWall("Sign in to confirm you're not a bot")

    with pytest.raises(I._BotWall):
        I._with_format_fallback(Config.load(), attempt)


# --- the Settings "Test" button must agree with the download ----------------- #

class _FakeYDL:
    """Minimal yt_dlp.YoutubeDL stand-in: refuses every format except `ok_fmt`."""
    ok_fmt = "best"
    seen: list[str] = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False, process=True):
        fmt = self.opts.get("format")
        type(self).seen.append(fmt)
        if fmt is not None and fmt != self.ok_fmt:
            raise Exception("ERROR: [youtube] z0: Requested format is not available")
        return {"title": "My video", "duration": 610.0,
                "formats": [{"format_id": "18", "width": 640, "height": 360,
                             "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"}]}


@pytest.fixture
def _fake_ytdlp(monkeypatch):
    _FakeYDL.seen = []
    monkeypatch.setattr(I, "_require_ytdlp",
                        lambda: type("M", (), {"YoutubeDL": _FakeYDL}))
    return _FakeYDL


def test_auth_test_retries_looser_formats_like_the_download_does(_fake_ytdlp):
    """The operator saw '✗ firefox: SIGNED IN OK (no matching video format)' while
    the download would have succeeded. A ✓ here must mean the download works."""
    cfg = Config.load()
    cfg.override("ingest.cookies_from_browser", "firefox")
    ok, detail = I.test_youtube_auth("https://youtu.be/z0", cfg)
    assert ok is True
    assert "✓ firefox browser cookies" in detail
    assert "My video" in detail
    assert "looser format" in detail                   # says which one saved it
    assert _FakeYDL.seen[:2] == I._format_chain(cfg)[:2]   # it really did loosen


def test_auth_test_still_fails_loudly_when_nothing_works(_fake_ytdlp, monkeypatch):
    monkeypatch.setattr(_FakeYDL, "ok_fmt", "no-such-format")
    cfg = Config.load()
    ok, detail = I.test_youtube_auth("https://youtu.be/z0", cfg)
    assert ok is False and detail.startswith("✗")
    assert "no downloadable format" in detail          # not blamed on auth


def test_list_formats_reports_what_youtube_offers(_fake_ytdlp):
    ok, detail = I.list_formats("https://youtu.be/z0", Config.load())
    assert ok is True and "1 format(s)" in detail and "640x360" in detail
    assert _FakeYDL.seen == [None]                     # never selects a format


def test_list_formats_flags_an_extractor_break(_fake_ytdlp, monkeypatch):
    monkeypatch.setattr(_FakeYDL, "extract_info",
                        lambda self, url, download=False, process=True: {"formats": []})
    ok, detail = I.list_formats("https://youtu.be/z0", Config.load())
    assert ok is False and "0 formats" in detail and "Update yt-dlp" in detail
