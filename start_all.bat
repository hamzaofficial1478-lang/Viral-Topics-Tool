@echo off
REM ===========================================================================
REM  ShortForge - everything: the dashboard UI + ntfy remote control + the
REM  queue worker. This is the one to run at logon.
REM
REM  Nothing starts processing on its own: on open you'll get an ntfy message
REM  ("ShortForge UI is open...") and, if anything is queued or was left
REM  mid-job, it asks permission before starting - reply "start" or "pause".
REM ===========================================================================
cd /d "%~dp0"
if not exist "venv\Scripts\activate.bat" (
  echo venv not found. Run setup.bat first.
  pause
  exit /b 1
)
REM A venv whose Python has been moved or uninstalled still HAS activate.bat,
REM so the check above passes and every later command silently falls back to
REM the system Python -- which has none of the packages. That produced an
REM endless "Listener stopped - restarting in 30s" loop with the real cause
REM ("did not find executable at ...python.exe") scrolled off the top.
"venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if ERRORLEVEL 1 (
  echo.
  echo   The virtual environment is broken - it points at a Python that is no
  echo   longer installed on this PC.
  echo.
  echo   Fix: delete the "venv" folder in this directory, then run setup.bat
  echo   again. It will rebuild it.
  echo.
  pause
  exit /b 1
)
call "venv\Scripts\activate.bat"

REM Dashboard in the background + open the browser at it.
start "ShortForge UI" /min cmd /c "python cli.py ui"
start "" /b cmd /c "ping -n 6 127.0.0.1 >nul & start http://localhost:8501"

REM ntfy listener in THIS window (restarts itself if it drops).
:loop
python cli.py listen --owner-confirmed
if %ERRORLEVEL% EQU 0 goto :done
if %ERRORLEVEL% EQU 2 goto :notconfigured
echo Listener stopped - restarting in 30s. Close this window to stop everything.
ping -n 31 127.0.0.1 >nul
goto :loop

:notconfigured
REM Exit code 2 means nothing is set up yet - retrying forever would just spam.
echo.
echo   Notifications are not configured yet, so there is nothing to listen to.
echo   Open the dashboard (it should already be on http://localhost:8501),
echo   go to Settings - Notifications, and set up an ntfy command topic. Then
echo   run this file again.
echo.
pause
exit /b 2

:done
echo Stopped.
pause
