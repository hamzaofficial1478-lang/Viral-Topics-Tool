@echo off
REM ===========================================================================
REM  ShortForge - one-click launch of the settings + new-job dashboard.
REM  Double-click this file (shows a console with the server log), or use
REM  start_ui.vbs for the same thing with no console window.
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

REM Open the browser a few seconds after Streamlit starts listening.
REM (The dashboard runs headless - see .streamlit\config.toml - so we open it
REM  ourselves; ping is a reliable, input-free way to wait ~4 seconds.)
start "" /b cmd /c "ping -n 5 127.0.0.1 >nul & start http://localhost:8501"

python cli.py ui
