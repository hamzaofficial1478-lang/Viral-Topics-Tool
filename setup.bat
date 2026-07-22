@echo off
REM ===========================================================================
REM  ShortForge - one-click setup for a fresh Windows PC.
REM  Clone the repo, then double-click this file. It sets everything up
REM  unattended and prints PASS/FAIL at the end. Handles paths with spaces.
REM ===========================================================================
setlocal enabledelayedexpansion
title ShortForge setup
cd /d "%~dp0"

echo ============================================================
echo   ShortForge setup
echo   Folder: "%CD%"
echo   (this window stays open at the end so you can read it)
echo ============================================================
echo.

REM ---- 1. Python -----------------------------------------------------------
REM  Prefer the "py" launcher (avoids the Microsoft Store python stub).
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [FAIL] Python is not installed, or not on PATH.
  echo        Install Python 3.11 or newer ^(64-bit^) from:
  echo            https://www.python.org/downloads/windows/
  echo        On the FIRST installer screen tick "Add python.exe to PATH",
  echo        finish, then run this setup.bat again.
  goto :fail
)
echo [ok ] Python found:
%PY% --version
echo.

REM ---- 2. Virtual environment + Python packages ---------------------------
if not exist "venv\Scripts\activate.bat" (
  echo [..] Creating virtual environment "venv" ...
  %PY% -m venv venv
  if !ERRORLEVEL! NEQ 0 (
    echo [FAIL] Could not create the virtual environment.
    goto :fail
  )
) else (
  echo [ok ] venv already exists.
)
call "venv\Scripts\activate.bat"
echo [..] Upgrading pip ...
python -m pip install --upgrade pip
echo [..] Installing requirements ^(a few minutes on first run^) ...
python -m pip install -r requirements.txt
if !ERRORLEVEL! NEQ 0 (
  echo [FAIL] pip install failed - scroll up for the error.
  goto :fail
)
echo [ok ] Python packages installed.
echo.

REM ---- 3. FFmpeg -----------------------------------------------------------
where ffmpeg >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
  echo [..] FFmpeg not found - installing with winget ...
  where winget >nul 2>&1
  if !ERRORLEVEL! NEQ 0 (
    echo [warn] winget is not available on this PC. Install FFmpeg manually:
    echo        https://www.gyan.dev/ffmpeg/builds/  ^(add its bin\ folder to PATH^)
  ) else (
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    echo [note] FFmpeg was just installed. If the check at the end says ffmpeg is
    echo        missing, CLOSE this window and run setup.bat again - a new window
    echo        picks up the updated PATH.
  )
) else (
  echo [ok ] FFmpeg found on PATH.
)
echo.

REM ---- 4. Visual C++ runtime (x64) - needed by faster-whisper -------------
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
  echo [..] Microsoft Visual C++ runtime missing - downloading and installing ...
  set "VCR=%TEMP%\vc_redist.x64.exe"
  curl -L -o "!VCR!" https://aka.ms/vs/17/release/vc_redist.x64.exe
  if exist "!VCR!" (
    "!VCR!" /install /quiet /norestart
    echo [ok ] VC++ runtime installer finished.
  ) else (
    echo [warn] Could not download it automatically. Get it here and run it:
    echo        https://aka.ms/vs/17/release/vc_redist.x64.exe
  )
) else (
  echo [ok ] Visual C++ runtime present.
)
echo.

REM ---- 5. Pre-download the Whisper "small" model --------------------------
echo [..] Caching the Whisper "small" model so the first real run is fast ...
python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8'); print('whisper small ready')"
if !ERRORLEVEL! NEQ 0 (
  echo [warn] Model pre-download failed - it will download on the first run instead.
) else (
  echo [ok ] Whisper "small" cached.
)
echo.

REM ---- 6. Final environment check ----------------------------------------
echo ============================================================
echo   Environment check  ^(python cli.py doctor^)
echo ============================================================
python cli.py doctor
set "DOC=!ERRORLEVEL!"
echo.
if "!DOC!"=="0" (
  echo ############################################################
  echo #  SETUP RESULT: PASS  -  ShortForge is ready.
  echo #
  echo #  Next:
  echo #   1^) Double-click start_ui.bat ^(or start_ui.vbs for no console^).
  echo #   2^) In the browser, open Settings.
  echo #   3^) Import your shortforge-settings.json to restore your API keys.
  echo ############################################################
  goto :done
)
echo ############################################################
echo #  SETUP RESULT: FAIL  -  see the doctor lines above.
echo #  Most common cause: FFmpeg was just installed this session.
echo #  Fix: close this window and run setup.bat again ^(PATH refreshes^).
echo ############################################################
goto :done

:fail
echo.
echo ############################################################
echo #  SETUP RESULT: FAIL  -  fix the item above and re-run setup.bat.
echo ############################################################

:done
echo.
pause
endlocal
