@echo off
REM ===========================================================================
REM  ShortForge - move this program folder somewhere else (C:\ShortForge unless
REM  you name another place), e.g. out of OneDrive, which opens every file it
REM  syncs and slows ShortForge down.
REM
REM    Double-click it, or in PowerShell (inside the current ShortForge folder):
REM        .\move_to_c.bat
REM    Somewhere else:
REM        .\move_to_c.bat D:\ShortForge
REM
REM  It COPIES - the old folder stays until you delete it yourself - and works
REM  out both paths on its own, so nothing has to be typed exactly right. Your
REM  settings and keys, channels, download history, queue and clips come along.
REM  It then checks the copy, points start-at-logon at the new place, and runs
REM  setup.bat there.
REM ===========================================================================
setlocal enabledelayedexpansion
title ShortForge - move folder

set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "DST=%~1"
if not defined DST set "DST=C:\ShortForge"
if "%DST:~-1%"=="\" set "DST=%DST:~0,-1%"

echo ============================================================
echo   Move ShortForge
echo   From: "%SRC%"
echo   To:   "%DST%"
echo ============================================================
echo.

if not exist "%SRC%\cli.py" (
  echo [FAIL] Run this file from inside the ShortForge folder - cli.py is not next to it.
  goto :end
)
if /i "%SRC%"=="%DST%" (
  echo [ok ] ShortForge is already in "%DST%" - nothing to move.
  goto :end
)
if "%DST:~1%"==":" (
  echo [FAIL] Give a folder, not a whole drive - for example  C:\ShortForge
  goto :end
)
if not "%DST:~1,2%"==":\" (
  echo [FAIL] Give the full path of the new folder - for example  C:\ShortForge
  goto :end
)
set "TEST=%DST%\"
if /i not "!TEST:%SRC%\=!"=="!TEST!" (
  echo [FAIL] The new place can't be inside the current folder.
  goto :end
)

REM ---- 1. ShortForge must not be running ------------------------------------
tasklist /FI "IMAGENAME eq python.exe" 2>nul | find /I "python.exe" >nul
if !ERRORLEVEL! EQU 0 (
  echo [warn] Python is running - ShortForge is probably still open, or a Shorts
  echo      download or channel search is still going in the background.
  echo      Close ShortForge's windows first. To stop every Python program, run
  echo      this in PowerShell:   Get-Process python* ^| Stop-Process -Force
  echo.
  choice /C YN /M "Copy anyway"
  if !ERRORLEVEL! NEQ 1 goto :end
)

REM ---- 2. What is already at the destination? -------------------------------
REM  An earlier copy that landed one level down (C:\ShortForge\[folder]\...) is
REM  the usual reason setup.bat "is not recognized" in the new place.
set "PARTIAL="
for %%P in (cli.py setup.bat shortforge\ .git\) do if exist "%DST%\%%P" set "PARTIAL=1"
if defined PARTIAL (
  echo [..] "%DST%" already has ShortForge ^(or part of it^) - completing the copy.
  goto :copy
)
if not exist "%DST%\" goto :copy
set "NESTED="
for /d %%D in ("%DST%\*") do if exist "%%D\cli.py" set "NESTED=%%D"
if defined NESTED (
  echo [warn] An earlier copy went one folder too deep:
  echo        "!NESTED!"
  echo      so "%DST%" itself has no setup.bat. That is only a copy - the
  echo      original is still in "%SRC%".
  echo.
  choice /C YN /M "Delete that misplaced copy and copy again properly"
  if !ERRORLEVEL! NEQ 1 goto :end
  rmdir /s /q "!NESTED!"
)
set "HAS="
for /f "delims=" %%F in ('dir /b /a "%DST%" 2^>nul') do set "HAS=1"
if not defined HAS goto :copy
echo [FAIL] "%DST%" already exists and holds other things:
dir /b /a "%DST%"
echo.
echo        Nothing was changed. Pick another folder, for example:
echo            .\move_to_c.bat C:\ShortForge2
goto :end

REM ---- 3. Copy ----------------------------------------------------------------
:copy
echo [..] Copying. Left out on purpose: the Python environment ^(setup.bat builds
echo      a fresh one - quicker and safer than copying it^) and cached source
echo      videos ^(downloaded again if a job needs them^).
echo.
robocopy "%SRC%" "%DST%" /E /XJ /R:2 /W:2 /NFL /NDL /NP /XD "%SRC%\venv" "%SRC%\.venv" "%SRC%\.shortforge\downloads" __pycache__ .pytest_cache
set "RC=!ERRORLEVEL!"
if !RC! GEQ 8 (
  echo.
  echo [FAIL] Some files could not be copied ^(robocopy code !RC!^) - see FAILED above.
  echo        Close ShortForge, wait for OneDrive to finish syncing, run this again.
  goto :end
)
if not exist "%DST%\setup.bat" (
  echo [FAIL] The copy has no setup.bat - something stopped it. Nothing was deleted.
  goto :end
)
if not exist "%DST%\cli.py" (
  echo [FAIL] The copy has no cli.py - something stopped it. Nothing was deleted.
  goto :end
)
echo.
echo [ok ] Copied to "%DST%".

REM ---- 4. Start-at-logon pointed at the old folder: point it at the new one ---
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "EV=%STARTUP%\ShortForge Everything.bat"
set "QU=%STARTUP%\ShortForge Queue.bat"
if exist "%EV%" (
  > "%EV%" echo @echo off
  >>"%EV%" echo REM Auto-generated by install_autostart.bat - delete this file to turn it off.
  >>"%EV%" echo cd /d "%DST%"
  >>"%EV%" echo call "%DST%\start_all.bat"
  echo [ok ] Start-at-logon now starts the copy in "%DST%".
)
if exist "%QU%" (
  > "%QU%" echo @echo off
  >>"%QU%" echo REM Auto-generated by install_autostart.bat - delete this file to turn it off.
  >>"%QU%" echo cd /d "%DST%"
  >>"%QU%" echo call "%DST%\run_queue.bat"
  echo [ok ] Start-at-logon now starts the copy in "%DST%".
)

REM ---- 5. A "ShortForge" icon on the desktop that opens the NEW place -------
REM  The program itself must not live on the Desktop when OneDrive backs the
REM  Desktop up (C:\Users\[you]\OneDrive\Desktop) - that is still OneDrive.
REM  A shortcut is a tiny file that only points at the real folder.
set "VBS=%TEMP%\shortforge_shortcut.vbs"
> "%VBS%" echo Set sh = CreateObject("WScript.Shell")
>>"%VBS%" echo Set lnk = sh.CreateShortcut(sh.SpecialFolders("Desktop") ^& "\ShortForge.lnk")
>>"%VBS%" echo lnk.TargetPath = "wscript.exe"
>>"%VBS%" echo lnk.Arguments = """%DST%\start_ui.vbs"""
>>"%VBS%" echo lnk.WorkingDirectory = "%DST%"
>>"%VBS%" echo lnk.Description = "ShortForge dashboard"
>>"%VBS%" echo lnk.Save
cscript //nologo "%VBS%" >nul 2>&1
set "SC=!ERRORLEVEL!"
del "%VBS%" >nul 2>&1
if "!SC!"=="0" (
  echo [ok ] A "ShortForge" icon on your desktop now opens "%DST%".
) else (
  echo [warn] Couldn't make the desktop icon - right-click "%DST%\start_ui.vbs" and
  echo        choose Send to - Desktop ^(create shortcut^) instead.
)

REM ---- 6. Set up the new copy -------------------------------------------------
echo.
echo [..] Setting up the new copy - setup.bat runs next ^(a few minutes^) ...
echo.
call "%DST%\setup.bat"

echo.
echo ############################################################
echo #  From now on ShortForge lives in:  "%DST%"
echo #
echo #  Start it - double-click the ShortForge icon on your desktop,
echo #  or in PowerShell:
echo #      cd "%DST%"
echo #      .\start_ui.bat
echo #
echo #  Once it works, delete the old copy - in PowerShell:
echo #      Remove-Item "%SRC%" -Recurse -Force
echo #
echo #  Any OTHER shortcut you made earlier still opens the old folder -
echo #  delete it and use the new ShortForge icon.
echo ############################################################

:end
echo.
pause
endlocal
