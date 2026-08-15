"""Which build of ShortForge is this machine actually running?

The operator runs the same program on two PCs and pulls to each by hand, so the
two drift. That drift is invisible and it wastes rounds: a 403 download log they
sent was produced by code from before the fix for it had landed, and the fix
looked broken because the machine reporting the failure had never pulled it.
Nothing on screen could have told either of us that.

So: one short build id, shown in the dashboard footer and printed by `doctor`.
"Is this PC on the new code?" becomes a glance instead of an argument.

Read from git, because that is what the operator actually pulls with. Falls back
to reading .git/HEAD directly (no git binary needed), then to "unknown" — this
is a label, and it must never be able to fail a run.
"""

from __future__ import annotations

import os
import subprocess

_cached: str | None = None


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _from_git(root: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", root, "log", "-1", "--format=%h %cs"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:      # noqa: BLE001 - no git on PATH, not a checkout, slow disk
        pass
    return None


def _from_head_file(root: str) -> str | None:
    """SHA without invoking git — a zip download or a PATH without git still
    gets an id, just no date."""
    try:
        head = os.path.join(root, ".git", "HEAD")
        with open(head, encoding="utf-8") as fh:
            ref = fh.read().strip()
        if ref.startswith("ref: "):
            name = ref[5:]
            direct = os.path.join(root, ".git", *name.split("/"))
            if os.path.isfile(direct):
                with open(direct, encoding="utf-8") as fh:
                    return fh.read().strip()[:7]
            packed = os.path.join(root, ".git", "packed-refs")
            if os.path.isfile(packed):
                with open(packed, encoding="utf-8") as fh:
                    for line in fh:
                        if line.rstrip().endswith(" " + name):
                            return line.split()[0][:7]
            return None
        return ref[:7] or None
    except Exception:      # noqa: BLE001
        return None


def build_id(refresh: bool = False) -> str:
    """Short commit id (+ date when git can supply it), e.g. ``90198b4 2026-08-15``."""
    global _cached
    if _cached is not None and not refresh:
        return _cached
    root = _repo_root()
    _cached = _from_git(root) or _from_head_file(root) or "unknown"
    return _cached
