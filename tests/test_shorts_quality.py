"""Shorts download quality — "no compromise on video quality".

The operator's Shorts came out pixelated. Reproduced with a stand-in for
YouTube's media servers (tests at the bottom), the old downloader:
  * abandoned a stream YouTube cut off part-way instead of resuming it, and
    rotated to fallback routes that offered only small versions;
  * ended its format list at "any single file" (often 360p on YouTube);
  * silently SKIPPED stream pieces it couldn't fetch — saving "done" files with
    half the video missing;
  * could resume a failed HD attempt's partial file with a SD stream — one file
    that starts 1080p and carries on 360p.
"""

import http.server
import importlib
import json
import os
import shutil
import subprocess
import threading

import pytest

from shortforge.config import Config
from shortforge.shorts import store as S
from shortforge.shorts import youtube as YT
from shortforge.utils import ShortForgeError

I = importlib.import_module("shortforge.ingest.ingest")


# --- resume a stream that was cut off, don't abandon it ----------------------- #

class _FakeYDL:
    """Fails with a 403 after sending some bytes, `fail_times` times."""
    fail_times = 1
    calls = 0
    bytes_before_cut = 5_000_000

    def __init__(self, opts):
        self.hooks = opts.get("progress_hooks") or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        type(self).calls += 1
        for h in self.hooks:
            h({"status": "downloading", "downloaded_bytes": self.bytes_before_cut,
               "info_dict": {"height": 1920}})
        if type(self).calls <= type(self).fail_times:
            raise Exception("ERROR: unable to download video data: HTTP Error 403: Forbidden")
        return {"id": "x", "ext": "mp4"}

    def prepare_filename(self, info):
        return "/tmp/x.mp4"


@pytest.fixture
def fake_ydl(monkeypatch):
    _FakeYDL.calls = 0
    monkeypatch.setattr(I, "_require_ytdlp", lambda: type("M", (), {"YoutubeDL": _FakeYDL}))
    monkeypatch.setattr("time.sleep", lambda s: None)
    return _FakeYDL


def test_a_stream_cut_off_midway_is_resumed_not_abandoned(fake_ydl):
    cfg = Config.load()
    cfg.override("ingest.retries", 3)
    info, _ = I._download_with_retries("u", {}, cfg)
    assert info["id"] == "x" and fake_ydl.calls == 2          # resumed once, then done


def test_a_refusal_before_any_data_still_rotates_the_client(fake_ydl, monkeypatch):
    """0 bytes = this client is refused. Retrying it would waste time; the
    player-client rotation is the right response there."""
    monkeypatch.setattr(_FakeYDL, "bytes_before_cut", 0)
    with pytest.raises(I._Forbidden):
        I._download_with_retries("u", {}, Config.load())
    assert fake_ydl.calls == 1


def test_a_stream_that_keeps_getting_cut_gives_up_after_the_retries(fake_ydl, monkeypatch):
    monkeypatch.setattr(_FakeYDL, "fail_times", 99)
    cfg = Config.load()
    cfg.override("ingest.retries", 3)
    with pytest.raises(I._Forbidden):
        I._download_with_retries("u", {}, cfg)
    assert fake_ydl.calls == 3


def test_a_stream_piece_that_never_arrives_is_a_cut_off():
    e = Exception("ERROR: fragment 3 not found, unable to continue")
    assert isinstance(I._classify(e), I._Forbidden)


# --- only the best, unless lower is explicitly allowed ------------------------ #

_FMTS = [{"format_id": "18", "vcodec": "avc1", "width": 360, "height": 640, "url": "u"},
         {"format_id": "22", "vcodec": "avc1", "width": 720, "height": 1280, "url": "u"},
         {"format_id": "137", "vcodec": "avc1", "width": 1080, "height": 1920, "url": "u",
          "vbr": 3000},
         {"format_id": "248", "vcodec": "vp9", "width": 1080, "height": 1920, "url": "u",
          "vbr": 2000}]


def test_step_down_goes_one_size_at_a_time():
    best = YT.best_offered(_FMTS, {})
    sels = YT.step_down_selectors(_FMTS, best, {})
    assert sels == ["bv*[width<=720][height<=1280]+ba/b[width<=720][height<=1280]",
                    "bv*[width<=360][height<=640]+ba/b[width<=360][height<=640]"]


def test_format_table_marks_what_will_be_downloaded():
    rows, best = YT.format_table(_FMTS, {})
    assert rows[0]["size"] == "1080×1920" and best["short"] == 1080
    assert any("will be downloaded" in r["note"] for r in rows)


def test_every_format_gets_its_own_partial_file(tmp_path):
    """One shared name let a SD retry resume the HD attempt's .part."""
    opts = YT.base_opts(Config.load(), {}, str(tmp_path))
    assert "%(format_id)s" in opts["outtmpl"]


def _patch_download(monkeypatch, tmp_path, *, fail_strict: bool, deliver=(1080, 1920)):
    """download_resilient stand-in: writes a real (tiny) video of `deliver` size."""
    calls = []
    monkeypatch.setattr(YT, "probe_formats", lambda url, cfg: _FMTS)
    def fake(url, cfg, opts, formats=None):
        calls.append(formats)
        if fail_strict and len(calls) == 1:
            raise ShortForgeError("YouTube would not serve it to any player client")
        stg = os.path.dirname(opts["outtmpl"])
        path = os.path.join(stg, "vid.137.mp4")
        w, h = deliver if len(calls) == 1 else (720, 1280)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=gray:s={w}x{h}:d=1", "-c:v", "libx264", path], check=True)
        return {"id": "vid", "title": "T", "requested_downloads": [{"filepath": path}]}, path
    monkeypatch.setattr(I, "download_resilient", fake)
    monkeypatch.setattr(S, "data_dir", lambda cfg=None: str(tmp_path / "data"))
    cfg = Config.load()
    cfg.override("paths.output_dir", str(tmp_path / "out"))
    return cfg, calls


needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg not installed")


@needs_ffmpeg
def test_best_quality_or_nothing_by_default(monkeypatch, tmp_path):
    cfg, calls = _patch_download(monkeypatch, tmp_path, fail_strict=True)
    item = {"id": "vidvidvid01", "url": "u", "channel_name": "C", "source": "link"}
    with pytest.raises(ShortForgeError, match="best quality"):
        YT.download_short(item, cfg, {**S.DEFAULT_SETTINGS})
    assert calls[0] == [YT.strict_selector(YT.best_offered(_FMTS, {}))]
    assert len(calls) == 1                                      # no lower-quality attempt
    assert not os.path.exists(tmp_path / "out")                 # nothing saved


@needs_ffmpeg
def test_lower_quality_only_when_allowed_and_then_labelled(monkeypatch, tmp_path):
    cfg, calls = _patch_download(monkeypatch, tmp_path, fail_strict=True)
    item = {"id": "vidvidvid01", "url": "u", "channel_name": "C", "source": "link"}
    rec = YT.download_short(item, cfg, {**S.DEFAULT_SETTINGS, "allow_lower_quality": True})
    assert calls[1][0].startswith("bv*[width<=720]")            # stepped down one size
    assert (rec["width"], rec["height"]) == (720, 1280)
    assert "best on offer was 1080×1920" in rec["quality_note"]


@needs_ffmpeg
def test_a_file_smaller_than_the_best_is_rejected_even_if_it_downloaded(monkeypatch, tmp_path):
    """The file is measured, not trusted: a smaller picture that got through is
    not saved as if it were the real thing."""
    cfg, _ = _patch_download(monkeypatch, tmp_path, fail_strict=False, deliver=(360, 640))
    item = {"id": "vidvidvid01", "url": "u", "channel_name": "C", "source": "link"}
    with pytest.raises(ShortForgeError, match="only 360×640"):
        YT.download_short(item, cfg, {**S.DEFAULT_SETTINGS})


# --- fixing Shorts that already came in pixelated ----------------------------- #

def test_redownload_keeps_the_number_and_is_not_skipped(tmp_path):
    dd = str(tmp_path / "d")
    rec = {"id": "aaaaaaaaaaa", "number": 7, "file": str(tmp_path / "7 - T.mp4"),
           "channel_key": "@c", "channel_name": "C", "title": "T"}
    assert S.queue_redownload(dd, [rec]) == 1
    assert S.queue_redownload(dd, [rec]) == 0                    # not queued twice
    item = S.load_run(dd)["items"][0]
    assert item["replace"] == {"number": 7, "file": rec["file"]}


# --- the real thing: yt-dlp against a server that behaves like YouTube -------- #

@pytest.fixture(scope="module")
def fake_youtube(tmp_path_factory):
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("hls")
    for name, size, br in (("hd", "720x1280", "3M"), ("sd", "180x320", "300k")):
        (root / name).mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc2=size={size}:rate=30:duration=8", "-f", "lavfi", "-i",
                        "sine=duration=8", "-c:v", "libx264", "-preset", "ultrafast",
                        "-b:v", br, "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-f", "hls",
                        "-hls_time", "2", "-hls_playlist_type", "vod",
                        str(root / name / "i.m3u8")], check=True)
    (root / "master.m3u8").write_text(
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=180x320,CODECS="avc1.64000d,mp4a.40.2"\n'
        "sd/i.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=3500000,RESOLUTION=720x1280,CODECS="avc1.64001f,mp4a.40.2"\n'
        "hd/i.m3u8\n")

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def translate_path(self, path):
            mode, _, rest = path.split("?")[0].strip("/").partition("/")
            self.mode = mode
            return str(root / rest)

        def do_GET(self):
            p = self.translate_path(self.path)
            name = os.path.basename(p)
            if self.mode == "cut" and "/hd/" in p and name.endswith(".ts") and name[1:-3] >= "2":
                return self.send_error(403, "Forbidden")
            return super().do_GET()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _run(base_url, mode, tmp_path, monkeypatch, allow=False):
    monkeypatch.setattr(S, "data_dir", lambda cfg=None: str(tmp_path / "data"))
    monkeypatch.setattr("time.sleep", lambda s: None)
    cfg = Config.load()
    cfg.override("paths.output_dir", str(tmp_path / "out"))
    cfg.override("ingest.retries", 2)
    item = {"id": "hlsshort001", "url": f"{base_url}/{mode}/master.m3u8",
            "channel_name": "C", "source": "link"}
    return YT.download_short(item, cfg, {**S.DEFAULT_SETTINGS, "allow_lower_quality": allow})


def _frame_sizes(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "frame=width,height", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout
    return {tuple(x.strip(",").split(",")[:2]) for x in out.split() if x.strip(",")}


def _frames(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "json", path],
                         capture_output=True, text=True).stdout
    return int(json.loads(out)["streams"][0]["nb_read_packets"])


def test_real_download_takes_the_hd_version(fake_youtube, tmp_path, monkeypatch):
    rec = _run(fake_youtube, "ok", tmp_path, monkeypatch)
    assert _frame_sizes(rec["file"]) == {("720", "1280")}
    assert _frames(rec["file"]) == 240                         # the whole 8 seconds


def test_real_cut_off_hd_stream_is_not_saved_broken_or_smaller(fake_youtube, tmp_path,
                                                               monkeypatch):
    """The old code saved this as a 'done' file with half the video missing."""
    with pytest.raises(ShortForgeError, match="best quality"):
        _run(fake_youtube, "cut", tmp_path, monkeypatch)
    out = tmp_path / "out"
    assert not out.exists() or not any(f for _, _, fs in os.walk(out) for f in fs)


def test_real_step_down_is_one_clean_stream_never_mixed(fake_youtube, tmp_path, monkeypatch):
    """The old code could resume the HD attempt's partial file with the SD
    stream: a file that is 720p for a while and then 180p."""
    rec = _run(fake_youtube, "cut", tmp_path, monkeypatch, allow=True)
    assert _frame_sizes(rec["file"]) == {("180", "320")}       # every frame one size
    assert _frames(rec["file"]) == 240
    assert "best on offer was 720×1280" in rec["quality_note"]
