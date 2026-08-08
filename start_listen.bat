@echo off
REM ===========================================================================
REM  ShortForge - remote-control mode, no UI window. Send links from your
REM  phone over ntfy; this PC does the work and messages you as each one
REM  finishes. Use start_all.bat instead if you also want the dashboard to
REM  open automatically.
REM
REM  To start this automatically at logon: just double-click
REM  install_autostart.bat and choose "Everything mode" (that one runs
REM  start_all.bat, which includes this listener plus the UI).
REM
REM  Only the ntfy command topic saved in Settings can command it, and
REM  nothing starts processing without an explicit "start" reply - see
REM  Settings - Notifications for the full command list.
REM ===========================================================================
cd /d "%~dp0"
if not exist "venv\Scripts\activate.bat" (
  echo venv not found. Run setup.bat first.
  pause
  exit /b 1
)
call "venv\Scripts\activate.bat"

:loop
python cli.py listen --owner-confirmed
if %ERRORLEVEL% EQU 0 goto :done
if %ERRORLEVEL% EQU 2 goto :notconfigured
echo Listener stopped unexpectedly - restarting in 30s. Close this window to stop.
ping -n 31 127.0.0.1 >nul
goto :loop

:notconfigured
echo.
echo   Nothing is configured to listen to yet. Start the dashboard
echo   (start_ui.bat), open Settings - Notifications, and set up an ntfy
echo   command topic. Then run this file again.
echo.
pause
exit /b 2

:done
echo Listener stopped.
pause
