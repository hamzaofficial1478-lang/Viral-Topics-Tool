@echo off
REM ===========================================================================
REM  ShortForge - one-click update. Double-click this on EVERY PC you run
REM  ShortForge on, then compare the build id it prints at the end. If the two
REM  ids differ, one machine is running older code -- which has already cost
REM  real time: a download failure was reported against a PC that had never
REM  pulled the fix for it, so the fix looked broken when it simply wasn't there.
REM ===========================================================================
setlocal enabledelayedexpansion
title ShortForge update
cd /d "%~dp0"

echo ============================================================
echo   ShortForge update
echo   Folder: "%CD%"
echo ============================================================
echo.

where git >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
  echo [FAIL] git is not installed, or not on PATH.
  echo        Install it from https://git-scm.com/download/win then re-run this.
  goto :end
)

REM ---- 1. Park local edits so the pull can never abort ----------------------
REM  A hand-edited tracked file (requirements.txt, config/settings.yaml) makes
REM  git refuse the merge outright and print an error that stops the update
REM  dead. Stashing keeps the edit -- nothing is discarded -- and lets the
REM  pull through; the message below says how to get it back.
set "STASHED="
git diff --quiet
if !ERRORLEVEL! NEQ 0 (
  echo [..] You have local edits to tracked files. Parking them so the pull works:
  git --no-pager diff --stat
  git stash push -m "shortforge update.bat auto-stash" >nul 2>&1
  if !ERRORLEVEL! EQU 0 (
    set "STASHED=1"
    echo [ok ] Parked. Nothing was lost - see the note at the end.
  ) else (
    echo [warn] Could not stash. The pull may fail; scroll up for the reason.
  )
  echo.
)

REM ---- 2. Pull whichever branch this checkout is on -------------------------
set "BRANCH="
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%b"
if not defined BRANCH (
  echo [FAIL] Could not work out which branch this folder is on.
  goto :end
)
echo [..] Pulling "!BRANCH!" ...
git pull origin "!BRANCH!"
if !ERRORLEVEL! NEQ 0 (
  echo.
  echo [FAIL] The pull did not finish - scroll up for git's reason.
  goto :end
)
echo [ok ] Code updated.
echo.

REM ---- 3. Dependencies -----------------------------------------------------
REM  Same full-path rule as setup.bat: if activate silently fails, a bare
REM  "python" is the SYSTEM one and packages land outside the venv.
set "VPY=%CD%\venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo [warn] No venv found. Run setup.bat first.
  goto :end
)
"%VPY%" -c "import sys" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
  echo [warn] The venv is broken ^(it points at a Python that is gone^).
  echo        Run setup.bat - it rebuilds the venv automatically.
  goto :end
)
echo [..] Installing any new requirements ...
"%VPY%" -m pip install -q -r requirements.txt
echo [..] Updating yt-dlp ^(YouTube breaks its extractors often^) ...
"%VPY%" -m pip install -q -U yt-dlp
echo [ok ] Dependencies up to date.
echo.

REM ---- 4. Say which build this PC is now on --------------------------------
echo ============================================================
"%VPY%" -c "from shortforge.version import build_id; print('  This PC is now on build', build_id())"
echo ============================================================
if defined STASHED (
  echo.
  echo  NOTE: your local edits were parked, not deleted. To put them back:
  echo            git stash pop
  echo        To look at them first:      git stash show -p
  echo        To throw them away:         git stash drop
)

:end
echo.
pause
endlocal
