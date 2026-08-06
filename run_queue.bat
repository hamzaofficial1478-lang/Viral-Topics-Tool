@echo off
REM ===========================================================================
REM  ShortForge - work through the link queue.
REM  Double-click to start, or let Windows run it at logon (see below) so a
REM  power cut simply resumes: interrupted links go back in the queue and
REM  already-rendered clips are skipped.
REM
REM  Run at every logon (one-time setup, no admin needed):
REM      schtasks /create /tn ShortForgeQueue /tr "\"%~f0\"" /sc onlogon /rl highest
REM  Remove it again with:
REM      schtasks /delete /tn ShortForgeQueue /f
REM ===========================================================================
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
  echo venv not found. Run setup.bat first.
  pause
  exit /b 1
)
call "venv\Scripts\activate.bat"

REM Keep retrying if the machine drops the network mid-queue; the queue file is
REM the source of truth, so re-entering is always safe.
:loop
python cli.py queue run --owner-confirmed
if %ERRORLEVEL% EQU 130 goto :done
python -c "import sys;from shortforge import queue as Q;sys.exit(0 if Q.next_pending(Q.load_queue()) else 1)"
if %ERRORLEVEL% EQU 0 (
  echo Pending links remain - retrying in 60s. Close this window to stop.
  ping -n 61 127.0.0.1 >nul
  goto :loop
)

:done
echo.
echo Queue finished. See out\ for the clips.
pause
