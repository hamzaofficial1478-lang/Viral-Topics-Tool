@echo off
REM ===========================================================================
REM  ShortForge - register (or remove) automatic start at logon.
REM  Just double-click this file. It works out its own folder, so there are no
REM  paths to type and spaces in the folder name are handled correctly.
REM ===========================================================================
setlocal
title ShortForge autostart
cd /d "%~dp0"

echo ============================================================
echo   ShortForge - start automatically when Windows starts
echo   Folder: "%CD%"
echo ============================================================
echo.
echo  Which mode should start at logon?
echo.
echo    1  Telegram mode   - send links from your phone; also works the queue
echo    2  Queue mode      - just work through links already in the queue
echo    3  Remove autostart - turn both of these off again
echo    4  Cancel
echo.
set "CHOICE="
set /p CHOICE=Type 1, 2, 3 or 4 then press Enter:

if "%CHOICE%"=="1" goto :telegram
if "%CHOICE%"=="2" goto :queue
if "%CHOICE%"=="3" goto :remove
goto :cancel

:telegram
set "TASK=ShortForgeTelegram"
set "TARGET=%~dp0start_telegram.bat"
goto :install

:queue
set "TASK=ShortForgeQueue"
set "TARGET=%~dp0run_queue.bat"
goto :install

:install
if not exist "%TARGET%" (
  echo [FAIL] Could not find "%TARGET%"
  echo        Run this from inside your ShortForge folder.
  goto :done
)
REM Remove any previous copy first so re-running is always safe.
schtasks /delete /tn "%TASK%" /f >nul 2>&1
schtasks /create /tn "%TASK%" /tr "\"%TARGET%\"" /sc onlogon
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo [FAIL] Could not create the scheduled task - see the message above.
  goto :done
)
echo.
echo [ok] "%TASK%" will now start automatically when you log in.
echo      Target: "%TARGET%"
echo.
echo  To test it right now without rebooting:
echo      schtasks /run /tn "%TASK%"
echo  To remove it later: run this file again and choose 3.
goto :done

:remove
schtasks /delete /tn "ShortForgeTelegram" /f >nul 2>&1
schtasks /delete /tn "ShortForgeQueue" /f >nul 2>&1
echo.
echo [ok] Autostart removed (both modes). ShortForge will no longer start by itself.
goto :done

:cancel
echo Cancelled - nothing changed.

:done
echo.
pause
endlocal
