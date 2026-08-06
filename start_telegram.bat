@echo off
REM ===========================================================================
REM  ShortForge - Telegram mode. Send links from your phone; this PC does the
REM  work and messages you as each one finishes.
REM
REM  Start automatically at every logon (one-time, no admin needed):
REM      schtasks /create /tn ShortForgeTelegram /tr "\"%~f0\"" /sc onlogon
REM  Remove it again:
REM      schtasks /delete /tn ShortForgeTelegram /f
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
echo Listener stopped unexpectedly - restarting in 30s. Close this window to stop.
ping -n 31 127.0.0.1 >nul
goto :loop

:done
echo Telegram listener stopped.
pause
