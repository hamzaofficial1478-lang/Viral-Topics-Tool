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
call "venv\Scripts\activate.bat"

REM Open the browser a few seconds after Streamlit starts listening.
REM (The dashboard runs headless - see .streamlit\config.toml - so we open it
REM  ourselves; ping is a reliable, input-free way to wait ~4 seconds.)
start "" /b cmd /c "ping -n 5 127.0.0.1 >nul & start http://localhost:8501"

python cli.py ui
