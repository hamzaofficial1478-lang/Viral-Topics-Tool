@echo off
REM ===========================================================================
REM  ShortForge - everything: the dashboard UI + the Telegram listener + the
REM  queue worker. This is the one to run at logon.
REM ===========================================================================
cd /d "%~dp0"
if not exist "venv\Scripts\activate.bat" (
  echo venv not found. Run setup.bat first.
  pause
  exit /b 1
)
call "venv\Scripts\activate.bat"

REM Dashboard in the background + open the browser at it.
start "ShortForge UI" /min cmd /c "python cli.py ui"
start "" /b cmd /c "ping -n 6 127.0.0.1 >nul & start http://localhost:8501"

REM Telegram listener in THIS window (restarts itself if it drops).
:loop
python cli.py telegram --owner-confirmed
if %ERRORLEVEL% EQU 0 goto :done
echo Listener stopped - restarting in 30s. Close this window to stop everything.
ping -n 31 127.0.0.1 >nul
goto :loop

:done
echo Stopped.
pause
