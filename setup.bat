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
REM The project is developed and tested on 3.11-3.13. A brand-new release
REM (3.14 at time of writing) often has no prebuilt wheels yet for the heavy
REM dependencies here -- faster-whisper, ctranslate2, torch -- so pip tries to
REM build them from source and fails in ways that look nothing like the real
REM cause. Warn plainly rather than let that happen silently.
for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo !PYVER! | findstr /b /c:"3.14" /c:"3.15" >nul && (
  echo.
  echo [!!] Python !PYVER! is newer than this project is tested against ^(3.11-3.13^).
  echo      Several dependencies may have no wheels for it yet and will fail to
  echo      install. If the install below fails, install Python 3.13 from
  echo      python.org, then delete the "venv" folder and re-run setup.bat.
  echo.
)
echo.

REM ---- 2. Virtual environment + Python packages ---------------------------
REM A venv records an ABSOLUTE path to the Python that built it. Move, upgrade
REM or uninstall that Python and every "python -m pip" inside the venv dies with
REM "did not find executable at ...\python.exe" -- while activate.bat is still
REM sitting there, so a test for the FILE reports a healthy venv and setup
REM skips the rebuild that would fix it. That is exactly how a fresh machine
REM ends up failing every install step and then "No module named 'yaml'".
REM So: prove the venv can actually run, and rebuild it when it can't.
set "VENV_OK="
if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" -c "import sys" >nul 2>&1
  if !ERRORLEVEL! EQU 0 set "VENV_OK=1"
)
if defined VENV_OK (
  echo [ok ] venv already exists and works.
) else (
  if exist "venv" (
    echo [!!] The existing venv is broken - it points at a Python that is no
    echo      longer installed. Rebuilding it from scratch ...
    rmdir /s /q "venv"
  ) else (
    echo [..] Creating virtual environment "venv" ...
  )
  %PY% -m venv venv
  if !ERRORLEVEL! NEQ 0 (
    echo [FAIL] Could not create the virtual environment.
    goto :fail
  )
  if not exist "venv\Scripts\python.exe" (
    echo [FAIL] The virtual environment was created but has no python.exe.
    goto :fail
  )
)
call "venv\Scripts\activate.bat"
REM Call the venv's python by full path: if activate silently fails, "python"
REM would be the SYSTEM one and packages would land outside the venv -- which
REM is why `cli.py doctor` reported "No module named 'yaml'" afterwards.
set "VPY=%CD%\venv\Scripts\python.exe"
echo [..] Upgrading pip ...
"%VPY%" -m pip install --upgrade pip
echo [..] Installing requirements ^(a few minutes on first run^) ...
"%VPY%" -m pip install -r requirements.txt
if !ERRORLEVEL! NEQ 0 (
  echo [FAIL] pip install failed - scroll up for the error.
  goto :fail
)
echo [ok ] Python packages installed.
echo [..] Updating yt-dlp to the latest ^(YouTube extractors break often^) ...
"%VPY%" -m pip install -U yt-dlp
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
"%VPY%" -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8'); print('whisper small ready')"
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
"%VPY%" cli.py doctor
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
