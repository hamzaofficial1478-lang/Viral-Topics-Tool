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


def test_extractor_args_default_queries_web_and_tv():
    """The 'web' client alone is the one YouTube PO-token-gates down to ~360p;
    'tv' isn't gated the same way, so it must be queried by default too."""
    args = I._extractor_args(Config.load())
    assert args == {"youtube": {"player_client": ["default", "tv"]}}


def test_extractor_args_is_configurable():
    cfg = Config.load()
    cfg.override("ingest.player_client", "default, android , tv")
    assert I._extractor_args(cfg) == {"youtube": {"player_client": ["default", "android", "tv"]}}


def test_extractor_args_blank_config_falls_back_to_the_safe_default():
    """Same convention as ingest.format: a blank override means "unset", not
    "query nothing" — an accidentally-cleared field must not silently regress
    to the PO-token-gated single-client behavior."""
    cfg = Config.load()
    cfg.override("ingest.player_client", "")
    assert I._extractor_args(cfg) == {"youtube": {"player_client": ["default", "tv"]}}


def test_base_opts_carries_extractor_args():
    cfg = Config.load()
    opts = I._base_opts(cfg, "/tmp/dl")
    assert opts["extractor_args"] == I._extractor_args(cfg)


# --- download speed: concurrent fragment downloads --------------------------- #
# yt-dlp's own default is 1 (serial) — the biggest lever for "the download
# itself is slow" once auth/format are working, since anything above ~360p is
# fragmented (DASH/HLS).

def test_concurrent_fragments_default_is_four():
    assert I._resolve_concurrent_fragments(Config.load()) == 4


def test_concurrent_fragments_config_override_wins():
    cfg = Config.load()
    cfg.override("ingest.concurrent_fragments", 8)
    assert I._resolve_concurrent_fragments(cfg) == 8


def test_concurrent_fragments_falls_back_to_the_store(tmp_path, monkeypatch):
    """Same fallback pattern as _resolve_auth: cfg wins, else whatever was
    saved from Settings -> YouTube authentication."""
    monkeypatch.setenv("SHORTFORGE_PROVIDERS_FILE", str(tmp_path / "p.json"))
    from shortforge.providers import store as S
    S.save_store({"providers": [], "credentials": [],
                  "youtube_auth": {"concurrent_fragments": 6}})
    assert I._resolve_concurrent_fragments(Config.load()) == 6


def test_concurrent_fragments_is_bounded_both_ways():
    """A typo (0, or an absurdly high number) never reaches yt-dlp verbatim —
    clamped to a sane [1, 16] range instead of silently doing nothing or
    hammering YouTube's edge servers."""
    cfg = Config.load()
    cfg.override("ingest.concurrent_fragments", 0)
    assert I._resolve_concurrent_fragments(cfg) == 4          # 0 is falsy -> default
    cfg.override("ingest.concurrent_fragments", 999)
    assert I._resolve_concurrent_fragments(cfg) == 16


def test_base_opts_carries_concurrent_fragments():
    cfg = Config.load()
    cfg.override("ingest.concurrent_fragments", 5)
    opts = I._base_opts(cfg, "/tmp/dl")
    assert opts["concurrent_fragment_downloads"] == 5


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
    opts_seen: list[dict] = []

    def __init__(self, opts):
        self.opts = opts
        type(self).opts_seen.append(opts)

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
    _FakeYDL.opts_seen = []
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


def test_list_formats_flags_po_token_gating_when_capped_below_480p(_fake_ytdlp):
    """The exact symptom reported: signed in fine, formats exist, but nothing
    above 360p — that's the client being gated, not the selector being wrong."""
    ok, detail = I.list_formats("https://youtu.be/z0", Config.load())
    assert ok is True
    assert "PO-token-gated" in detail
    assert "360p" in detail


def test_probe_calls_carry_the_same_extractor_args_as_downloads(_fake_ytdlp):
    cfg = Config.load()
    I.test_youtube_auth("https://youtu.be/z0", cfg)
    I.list_formats("https://youtu.be/z0", cfg)
    assert _FakeYDL.opts_seen                      # the fake was actually exercised
    for opts in _FakeYDL.opts_seen:
        assert opts["extractor_args"] == I._extractor_args(cfg)


# --- DRM was not recognised at all -------------------------------------------- #
# A DRM-protected video matched no branch of _classify, so it fell through EVERY
# auth strategy in turn and surfaced as a generic "Download failed after trying
# N auth strategy(ies)". selfheal then read that as "unknown", and the operator
# got "Something unexpected went wrong" plus an LLM guess -- with three
# pointless retries and no way to tell a video limit from a program bug.

@pytest.mark.parametrize("raw", [
    "ERROR: This video is DRM protected",
    "ERROR: [youtube] abc: The stream is encrypted and cannot be downloaded",
    "Requested format is not available. Video is protected by DRM",
])
def test_drm_errors_are_recognised(raw):
    assert isinstance(I._classify(Exception(raw)), I._DRMProtected)


def test_drm_fails_immediately_instead_of_trying_every_strategy(tmp_path, monkeypatch,
                                                                _stub_probe):
    """No cookie, client or format selector can decrypt a stream, so retrying
    is guaranteed waste."""
    calls = []

    def fake(url, opts, cfg):
        calls.append(1)
        raise I._DRMProtected("This video is DRM protected")

    monkeypatch.setattr(I, "_download_with_retries", fake)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    cfg.override("ingest.cookies_from_browser", "firefox")   # 2 strategies available

    with pytest.raises(ShortForgeError) as ei:
        I._ingest_url("https://youtu.be/x", cfg)

    assert len(calls) == 1, "kept retrying a stream that cannot be decrypted"
    assert "DRM-protected" in str(ei.value)
    assert "not a problem with ShortForge" in str(ei.value)   # names whose fault it is


def test_a_drm_failure_reads_as_the_link_not_a_program_bug():
    """End of the chain: it must reach the operator as 'this video', not as
    'Something unexpected went wrong'."""
    from shortforge import selfheal as SH
    err = ("This video is DRM-protected, so its stream cannot be downloaded. "
           "This is a restriction on the video itself")
    kind, summary, remedy = SH.classify(err)
    assert kind == "drm" and remedy is None
    assert SH.origin(err) == SH.LINK
    assert "this video, not the program" in SH.where_and_what(err)


# --- HTTP 403 on the media URL ------------------------------------------------ #
# A 403 arrives AFTER extraction succeeds: YouTube handed out a download URL and
# then refused the fetch. Neither the cookie chain nor the format chain can help,
# so both were burned through pointlessly and the job failed.

def test_403_is_classified_separately_from_a_missing_format():
    assert isinstance(
        I._classify(Exception("unable to download video data: HTTP Error 403: Forbidden")),
        I._Forbidden)


def test_403_recovers_by_switching_player_client(tmp_path, monkeypatch, _stub_probe):
    """The actual remedy: the URL was minted for a client YouTube then rejected,
    so a different client is what works."""
    tried = []

    def fake(url, opts, cfg):
        clients = opts["extractor_args"]["youtube"]["player_client"]
        tried.append(",".join(clients))
        if "ios" not in clients:
            raise I._Forbidden("unable to download video data: HTTP Error 403: Forbidden")
        d = tmp_path / "downloads"
        d.mkdir(exist_ok=True)
        (d / "v.mp4").write_bytes(b"x")
        return ({"id": "v", "title": "Recovered", "duration": 12.0}, str(d / "v.mp4"))

    monkeypatch.setattr(I, "_download_with_retries", fake)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))

    meta = I._ingest_url("https://youtu.be/x", cfg)

    assert meta.title == "Recovered"
    assert tried[0] == "default,tv" and "ios" in tried[-1]     # configured set first


def test_403_on_every_client_still_fails_cleanly(tmp_path, monkeypatch, _stub_probe):
    def always403(url, opts, cfg):
        raise I._Forbidden("HTTP Error 403: Forbidden")

    monkeypatch.setattr(I, "_download_with_retries", always403)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    with pytest.raises(ShortForgeError):
        I._ingest_url("https://youtu.be/x", cfg)


# --- the operator's real failure: no client rotation on "no formats" ---------- #
# Their log, verbatim:
#   format 'bv*[height<=1080]+ba/b[height<=1080]/b' not offered ... trying looser
#   format 'bv*+ba/b' not offered ... trying looser
#   format 'best[ext=mp4]/best' not offered ... trying looser
#   auth strategy 'firefox browser cookies': signed in fine, but YouTube offered
#     no usable video format for this link (47.5s)
#   auth strategy 'no cookies' ... HTTP Error 403: Forbidden
#
# Four format selectors were tried against ONE set of player clients, and the
# client was never rotated -- because rotation only triggered on 403. But "no
# formats offered" and "403 on the media URL" are two faces of one cause: the
# player client YouTube is willing to serve. Rotating the format fixes neither.

def test_no_formats_offered_rotates_the_player_client(tmp_path, monkeypatch, _stub_probe):
    tried = []

    def fake(url, opts, cfg):
        clients = opts["extractor_args"]["youtube"]["player_client"]
        tried.append(",".join(clients))
        # Exactly the operator's video: the default set is offered nothing at
        # all, whatever format selector is asked for.
        if not ({"mweb", "tv_simply", "android_vr", "android", "ios"} & set(clients)):
            raise I._NoFormat("Requested format is not available")
        d = tmp_path / "downloads"
        d.mkdir(exist_ok=True)
        (d / "v.mp4").write_bytes(b"x")
        return ({"id": "v", "title": "Recovered", "duration": 12.0}, str(d / "v.mp4"))

    monkeypatch.setattr(I, "_download_with_retries", fake)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))

    meta = I._ingest_url("https://youtu.be/03n11GA5_oo", cfg)
    assert meta.title == "Recovered"
    assert tried[0] == "default,tv", "the configured clients must still go first"
    assert len(set(tried)) > 1, "the player client was never rotated"


def test_client_is_the_outer_loop_not_the_format(tmp_path, monkeypatch, _stub_probe):
    """Nesting matters: the client decides which formats EXIST, so asking a
    dead client for four different selectors is four wasted round-trips."""
    order = []

    def fake(url, opts, cfg):
        clients = ",".join(opts["extractor_args"]["youtube"]["player_client"])
        order.append((clients, opts["format"]))
        if "ios" not in clients:
            raise I._NoFormat("Requested format is not available")
        d = tmp_path / "downloads"
        d.mkdir(exist_ok=True)
        (d / "v.mp4").write_bytes(b"x")
        return ({"id": "v", "title": "ok", "duration": 1.0}, str(d / "v.mp4"))

    monkeypatch.setattr(I, "_download_with_retries", fake)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    I._ingest_url("https://youtu.be/x", cfg)

    # The first client must exhaust its formats before the next client starts.
    first = order[0][0]
    assert all(c == first for c, _ in order[:len(I._format_chain(cfg))])


def test_only_player_clients_this_ytdlp_knows_are_sent():
    """An unknown client name is rejected by yt-dlp outright, which would turn
    a recoverable failure into a hard one."""
    known = I._known_clients()
    if not known:                      # yt-dlp absent or its table moved
        return
    assert set(I._client_fallbacks(Config.load())) <= known


def test_every_client_refusing_says_so_and_points_at_ytdlp(tmp_path, monkeypatch, _stub_probe):
    """The old wording -- 're-run to resume the partial download; if it's a
    network problem this often clears on retry' -- described neither the cause
    nor the cure, and sent the operator to retry a link that could not work."""
    def refuse(url, opts, cfg):
        raise I._NoFormat("Requested format is not available")

    monkeypatch.setattr(I, "_download_with_retries", refuse)
    cfg = Config.load()
    cfg.override("paths.work_dir", str(tmp_path))
    with pytest.raises(ShortForgeError) as ei:
        I._ingest_url("https://youtu.be/x", cfg)
    msg = str(ei.value).lower()
    assert "player client" in msg
    assert "yt-dlp" in msg
    assert "network problem" not in msg
