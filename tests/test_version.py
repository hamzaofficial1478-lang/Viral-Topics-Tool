"""The build id: answers "is this PC on the code I pulled?".

The operator runs ShortForge on two PCs and pulls to each by hand. When one
lags, nothing on screen says so — a 403 download log they sent had been
produced by code from before the fix for it existed, and the fix looked broken
because the machine reporting the failure had never pulled it.
"""

from shortforge import version as V


def test_build_id_reports_this_checkout():
    bid = V.build_id(refresh=True)
    assert bid and bid != "unknown"
    assert len(bid.split()[0]) >= 7          # a short SHA


def test_build_id_is_cached():
    first = V.build_id(refresh=True)
    assert V.build_id() is first


def test_falls_back_to_head_file_without_git(monkeypatch):
    """A zip download, or a PATH without git, still gets an id."""
    monkeypatch.setattr(V, "_from_git", lambda root: None)
    monkeypatch.setattr(V, "_cached", None)
    bid = V.build_id(refresh=True)
    assert bid != "unknown" and len(bid) == 7


def test_never_raises_when_nothing_can_be_read(monkeypatch, tmp_path):
    """It is a label. It must not be able to fail a run."""
    monkeypatch.setattr(V, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(V, "_from_git", lambda root: None)
    assert V.build_id(refresh=True) == "unknown"
