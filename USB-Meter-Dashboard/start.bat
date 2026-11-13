@echo off
title USB Meter Dashboard
cd /d "%~dp0"

if exist "%~dp0runtime\python.exe" goto bundled

where python >nul 2>nul
if errorlevel 1 goto trypy
python app.py %*
goto end

:trypy
where py >nul 2>nul
if errorlevel 1 goto nopython
py -3.14 app.py %*
goto end

:bundled
"%~dp0runtime\python.exe" app.py %*
goto end

:nopython
echo.
echo Python 3.14 was not found and the bundled runtime is missing.
echo Re-copy the whole folder, or install Python 3.14 with "Add to PATH".
echo.
pause
goto end

:end
if errorlevel 1 pause