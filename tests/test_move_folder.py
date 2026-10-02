"""move_to_c.bat — moving ShortForge out of OneDrive without typing paths.

The operator copied the folder by hand and `.\setup.bat` was "not recognized"
in the new place: PowerShell wasn't in a folder that had setup.bat. The script
works out both paths itself (%~dp0), copies, checks the copy, and sets it up.
Windows' cmd can't run here, so these pin down the parts that must not
regress.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bat(name):
    with open(os.path.join(ROOT, name), "rb") as f:
        raw = f.read()
    return raw, raw.decode("ascii").replace("\r\n", "\n")


def test_it_finds_its_own_folder_and_defaults_to_c():
    _, s = _bat("move_to_c.bat")
    assert 'set "SRC=%~dp0"' in s
    assert 'set "DST=C:\\ShortForge"' in s


def test_it_copies_without_the_python_environment_or_cached_videos():
    _, s = _bat("move_to_c.bat")
    line = next(ln for ln in s.splitlines() if ln.startswith("robocopy "))
    assert '"%SRC%\\venv"' in line and '"%SRC%\\.shortforge\\downloads"' in line
    assert "/E" in line and "/XJ" in line


def test_it_checks_the_copy_and_then_runs_setup_there():
    _, s = _bat("move_to_c.bat")
    copy_at = s.index("robocopy ")
    assert s.index('if not exist "%DST%\\setup.bat"', copy_at) < s.index('call "%DST%\\setup.bat"')
    assert "GEQ 8" in s                                   # robocopy failure codes


def test_it_never_deletes_the_destination_folder_wholesale():
    """Only a misplaced ShortForge copy inside it may be removed, after asking —
    a destination holding anything else is refused untouched."""
    _, s = _bat("move_to_c.bat")
    deletes = re.findall(r"(?im)^\s*rmdir .*$", s)
    assert deletes == ['  rmdir /s /q "!NESTED!"']
    assert "Nothing was changed" in s


def test_it_repoints_start_at_logon():
    _, s = _bat("move_to_c.bat")
    assert 'call "%DST%\\start_all.bat"' in s and 'call "%DST%\\run_queue.bat"' in s


def test_batch_files_use_windows_line_endings_where_labels_are_jumped_to():
    raw, _ = _bat("move_to_c.bat")
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_warnings_survive_delayed_expansion():
    """With enabledelayedexpansion, `!` is eaten: "[!!] warning" printed as "[] warning"."""
    for name in os.listdir(ROOT):
        if name.endswith(".bat"):
            _, s = _bat(name)
            if "enabledelayedexpansion" in s:
                assert not re.search(r"(?im)^\s*echo [^\n]*\[!+\]", s), name
