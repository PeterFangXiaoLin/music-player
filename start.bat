@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)

".venv\Scripts\python.exe" -c "import flask, yt_dlp" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)

".venv\Scripts\python.exe" app.py --open %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo Startup failed. Check the error above and ensure Python 3.11+ is available.
pause
exit /b 1
