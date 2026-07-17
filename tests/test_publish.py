"""M14 publishing sidecar."""

import os

from shortforge.publish import sidecar_text, write_sidecar, write_index


_MD = {
    "title": "The one habit that changed everything",
    "description": "Here is the single habit that made the difference.\n\n#focus #habits",
    "hashtags": ["#shorts", "#focus", "#habits"],
    "first_comment": "What's your #1 habit? 👇",
}


def test_sidecar_text_has_all_blocks():
    txt = sidecar_text(_MD)
    assert "TITLE" in txt and _MD["title"] in txt
    assert "DESCRIPTION" in txt
    assert "TAGS" in txt
    assert "#shorts #focus #habits" in txt
    assert "FIRST COMMENT" in txt


def test_sidecar_strips_duplicate_hashtag_block_from_description():
    txt = sidecar_text(_MD)
    # The description's trailing "#focus #habits" line is dropped so tags aren't
    # shown twice; the sentence stays.
    assert "Here is the single habit that made the difference." in txt
    # Only the TAGS line carries hashtags — the description block must not end
    # with a hashtag-only line.
    desc_block = txt.split("DESCRIPTION", 1)[1].split("TAGS", 1)[0]
    assert "#focus #habits" not in desc_block


def test_write_sidecar_next_to_video(tmp_path):
    video = tmp_path / "clip_en_01_20260101.mp4"
    video.write_bytes(b"x")
    path = write_sidecar(str(video), _MD)
    assert path == str(tmp_path / "clip_en_01_20260101.txt")
    assert os.path.isfile(path)
    assert _MD["title"] in open(path, encoding="utf-8").read()


def test_write_sidecar_none_when_no_metadata(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    assert write_sidecar(str(video), None) is None


def test_write_index(tmp_path):
    idx = {"total_clips": 3, "runs": []}
    p = write_index(idx, str(tmp_path), "batch_index.json")
    assert os.path.isfile(p)
    import json
    assert json.load(open(p))["total_clips"] == 3
