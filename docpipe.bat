@echo off
rem Starts docpipe on Windows. Double-click this file.
rem The first start sets everything up automatically (needs internet once).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :makevenv
".venv\Scripts\python" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo  The tool's environment was built with an old Python; rebuilding it...
    rmdir /s /q .venv
    goto :makevenv
)
goto :deps

:makevenv
rem Prefer slightly older Pythons: the newest release is often not yet
rem supported by all packages.
set "PY="
for %%V in (3.13 3.12 3.11 3.10) do (
    if not defined PY (
        py -%%V -c "exit()" >nul 2>nul
        if not errorlevel 1 set "PY=py -%%V"
    )
)
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 goto :nopython
echo  First-time setup: preparing the tool. This needs internet
echo  and can take a few minutes...
%PY% -m venv .venv
if errorlevel 1 (
    echo  Could not prepare the tool's environment. Try reinstalling Python.
    pause
    exit /b 1
)

:deps
".venv\Scripts\python" -c "import click, markitdown, google.genai" >nul 2>nul
if not errorlevel 1 goto :run
echo  Installing the tool's components (needs internet)...
".venv\Scripts\python" -m pip install --upgrade pip >nul 2>nul
".venv\Scripts\python" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  Installation failed. The two usual causes:
    echo    - No internet connection: connect and double-click this file again.
    echo    - Your Python is so new that some components do not support it yet.
    echo      Install Python 3.12 or 3.13 from https://www.python.org/downloads/
    echo      delete the .venv folder inside this tool's folder, and
    echo      double-click this file again.
    pause
    exit /b 1
)
echo  Components installed.
echo.

:run
set "PYTHONPATH=%~dp0src"
".venv\Scripts\python" -m docpipe %*
echo.
pause
exit /b 0

:nopython
echo  This tool needs Python 3.10 or newer, which was not found.
echo.
echo    1. Go to https://www.python.org/downloads/ and download Python.
echo    2. Run the installer. IMPORTANT: tick the box that says
echo       "Add python.exe to PATH" before clicking Install.
echo    3. Double-click this file again.
echo.
pause
exit /b 1
