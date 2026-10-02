"""Windows refuses to replace a file another program has open — the dashboard
reading a progress file, OneDrive uploading it, an antivirus scan — with
"[WinError 5] Access is denied". A channel search died on exactly that:

    The Search Failed: [WinError 5] Access Denied: '...\\shorts_data\\discover\\
    20261002-140647-d8cc.json.t7de69j3.tmp' -> '...20261002-140647-d8cc.json'

Those holds last milliseconds, so saving waits and tries again.
"""

import glob
import json
import os

import pytest

from shortforge import utils as U


def _locked_for(n, monkeypatch):
    """os.replace fails like Windows does while the file is held open, n times."""
    real = os.replace
    calls = {"n": 0}

    def replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= n:
            raise PermissionError(13, "Access is denied", dst)
        return real(src, dst)
    monkeypatch.setattr(U.os, "replace", replace)
    monkeypatch.setattr(U.time, "sleep", lambda s: None)
    return calls


def test_a_save_waits_for_a_file_held_open_by_another_program(tmp_path, monkeypatch):
    path = str(tmp_path / "job.json")
    calls = _locked_for(5, monkeypatch)
    U.write_json_atomic(path, {"ok": 1})
    assert json.load(open(path)) == {"ok": 1} and calls["n"] == 6
    assert glob.glob(str(tmp_path / "*.tmp")) == []              # no leftovers


def test_a_file_held_for_good_still_fails_loudly_and_cleans_up(tmp_path, monkeypatch):
    path = str(tmp_path / "job.json")
    _locked_for(10_000, monkeypatch)
    with pytest.raises(PermissionError):
        U.write_json_atomic(path, {"ok": 1})
    assert glob.glob(str(tmp_path / "*.tmp")) == []


def test_a_search_survives_a_progress_save_that_cannot_get_at_the_file(tmp_path, monkeypatch):
    """Progress saves are skipped when the file can't be had; the search goes
    on and its final result is written."""
    from shortforge.shorts import discover as D
    dd = str(tmp_path)
    job = D.new_job(dd, {"mode": "describe", "prompt": "street food", "count": 1,
                         "use_ai": False})
    real_save = D.save_job
    state = {"n": 0}

    def flaky(d, j):
        state["n"] += 1
        if j.get("status") not in D._FINAL and state["n"] % 2 == 0:
            raise PermissionError(13, "Access is denied")
        return real_save(d, j)
    monkeypatch.setattr(D, "save_job", flaky)

    def fake_run(J, cfg, stop):
        for i in range(6):
            J.update(stage=f"checking {i}")
            J.note(f"note {i}")
    monkeypatch.setattr(D, "_run", fake_run)
    from shortforge.config import Config
    out = D.run(dd, job["id"], Config.load())
    assert out["status"] == "done"
    assert D.load_job(dd, job["id"])["status"] == "done"
    assert len(D.load_job(dd, job["id"])["notes"]) == 6
