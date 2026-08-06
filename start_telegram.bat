@echo off
REM ===========================================================================
REM  ShortForge - Telegram mode. Send links from your phone; this PC does the
REM  work and messages you as each one finishes.
REM
REM  To start this automatically at logon: just double-click
REM  install_autostart.bat and choose "Telegram mode". It works out the paths
REM  itself (typing them by hand breaks when the folder name has a space).
REM
REM  Only the chat id saved in Settings can command it - other chats are ignored.
REM ===========================================================================
cd /d "%~dp0"
if not exist "venv\Scripts\activate.bat" (
  echo venv not found. Run setup.bat first.
  pause
  exit /b 1
)
call "venv\Scripts\activate.bat"

:loop
python cli.py telegram --owner-confirmed
if %ERRORLEVEL% EQU 0 goto :done
if %ERRORLEVEL% EQU 2 goto :notconfigured
echo Listener stopped unexpectedly - restarting in 30s. Close this window to stop.
ping -n 31 127.0.0.1 >nul
goto :loop

:notconfigured
echo.
echo   Nothing is configured to listen to yet. Start the dashboard
echo   (start_ui.bat), open Settings - Notifications, and set up ntfy
echo   or Telegram. Then run this file again.
echo.
pause
exit /b 2

:done
echo Telegram listener stopped.
pause
